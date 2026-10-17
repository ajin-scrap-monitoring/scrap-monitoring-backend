"""Height cm medians through the real receiver and existing profiles response, not a UI mock."""

import copy
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.app.core.config import Settings, get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.services.monitoring_v2_service import (
    build_snapshot,
    lidar_profiles,
    monitoring_source,
    raw_lidar_profiles,
)
from tests.test_height_fill_pipeline import START, frame, prepare, send
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_lidar_height import inputs
from tests.test_monitoring_v2 import login, status


async def bins_frame(client, clock, index, heights, *, second=None):
    now = START + timedelta(seconds=index if second is None else second)
    clock["now"] = now
    s = status(f"bins-status-{index}", now, index + 2)
    s["config_revision"] = "cfg-1"
    assert (await send(client, s)).status_code == 201
    _, m = inputs()
    m.update(message_id=f"bins-measurement-{index}", generated_at=now.isoformat())
    for number, sensor in enumerate(m["sensors"]):
        sensor["scan"].update(
            scan_id=f"bins-scan-{number}-{index}",
            acquired_at=now.isoformat(),
            acquired_monotonic_ns=str(int((index + 1) * 1_000_000_000)),
            sequence=index,
        )
        for i, point in enumerate(sensor["scan"]["points"]):
            height = heights[0 if i < 3 else 1]
            if height is None:
                point["calculation_reason_codes"] = ["SDK_INVALID_RANGE"]
            else:
                point["position_mm"]["z"] = round((height - 50) * 10)
    response = await send(client, m)
    assert response.status_code == 201, response.text
    return m


async def test_existing_height_fields_contain_cm_medians_and_no_ui_calculation(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    await login(client)
    for second in range(6):
        await frame(client, db, height_context, second, 99 if second == 5 else 50)
        response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
        assert response.status_code == 200
        profile = response.json()["profiles"][0]
        assert profile["unit"] == "cm"
        assert profile["valuePolicy"] == "SPATIAL_TEMPORAL_MEDIAN"
        assert set(profile["samples"][0]) == {"positionRatio", "height", "valid"}
        assert profile["average"] == (100 if second >= 4 else None)
        assert profile["minimum"] == profile["maximum"] == profile["average"]
        assert profile["samples"][0]["height"] == profile["average"]
    raw = await raw_lidar_profiles(db, monitoring_source("pit-demo"), height_context["now"])
    assert raw.profiles[0].average == 198
    assert profile["samples"][0]["height"] == 100


async def test_height_rise_and_collection_ratio_have_no_second_median_delay(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    for second in range(5, 8):
        _, snapshot = await frame(client, db, height_context, second, 95)
        page = await lidar_profiles(db, monitoring_source("pit-demo"), height_context["now"])
        expected = 190 if second == 7 else 100
        assert page.profiles[0].average == expected
        assert snapshot.summary.loadPercent == page.meanSectionFillPercent == expected / 2


async def test_bin_heights_are_filtered_before_spatial_average_and_ratio(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    # Median of raw averages would be 50 cm (25%). Bin medians are 100/100 cm (50%).
    for index, heights in enumerate([(0, 100), (0, 100), (100, 0), (100, 0), (100, 100)]):
        await bins_frame(client, height_context, index, heights)
    source = monitoring_source("pit-demo")
    page = await lidar_profiles(db, source, height_context["now"])
    assert [s["height"] for s in page.profiles[0].samples] == [100, 100]
    assert page.profiles[0].average == 100
    assert page.meanSectionFillPercent == 50
    snapshot = await build_snapshot(db, source, height_context["now"])
    assert snapshot.summary.loadPercent == page.meanSectionFillPercent


async def test_ten_observations_within_one_second_do_not_fill_five_second_window(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    source = monitoring_source("pit-demo")
    for index in range(10):
        await bins_frame(client, height_context, index, (100, 100), second=index / 10)
    page = await lidar_profiles(db, source, height_context["now"])
    assert page.profiles[0].average is None
    for second in range(1, 5):
        await bins_frame(client, height_context, 10 + second, (100, 100), second=second)
    assert (await lidar_profiles(db, source, height_context["now"])).profiles[0].average == 100


async def test_missing_bin_is_never_filled_from_old_height_and_recovers_after_five(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    source = monitoring_source("pit-demo")
    for second in range(5):
        await bins_frame(client, height_context, second, (100, 100))
    for second in range(5, 11):
        await bins_frame(client, height_context, second, (None, 100) if second == 5 else (100, 100))
        page = await lidar_profiles(db, source, height_context["now"])
        assert page.profiles[0].samples[0]["height"] == (100 if second == 10 else None)
        assert page.profiles[0].samples[1]["height"] == 100
        assert page.meanSectionFillPercent == (50 if second == 10 else None)


async def test_height_zero_is_valid_and_status_failure_clears_display_and_window(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    source = monitoring_source("pit-demo")
    for second in range(5):
        await bins_frame(client, height_context, second, (0, 0))
    page = await lidar_profiles(db, source, height_context["now"])
    assert page.profiles[0].average == 0
    assert page.profiles[0].samples[0]["valid"] and page.meanSectionFillPercent == 0
    now = START + timedelta(seconds=5)
    height_context["now"] = now
    s = status("missing-status", now, 99)
    s["config_revision"] = "cfg-1"
    s["sensors"][1]["data_state"] = "STALE"
    assert (await send(client, s)).status_code == 201
    page = await lidar_profiles(db, source, now)
    assert all(p.average is None for p in page.profiles)
    assert (await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))).payload == {}


async def test_get_duplicate_and_evidence_never_advance_height_window(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    await login(client)
    m = await bins_frame(client, height_context, 0, (100, 100))
    key = ("site-demo", "edge-demo", "height_window")
    original = copy.deepcopy((await db.get(EdgeV2Latest, key)).payload)
    assert (await send(client, m)).json()["duplicate"]
    evidence = copy.deepcopy(m)
    evidence.update(
        message_id="evidence-only",
        delivery_kind="EVIDENCE",
        report_interval_ms=None,
        selection_policy="REFERENCED_SCANS",
        pair_state="NOT_APPLICABLE",
        pair_delta_ms=None,
        assessment_ids=["synthetic-evidence"],
    )
    assert (await send(client, evidence)).status_code == 201
    for _ in range(3):
        assert (await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")).status_code == 200
    assert (await db.get(EdgeV2Latest, key)).payload == original


async def test_height_only_mode_keeps_edge_decisions_and_can_be_disabled(
    client, test_db_session, height_context, monkeypatch
):
    db = test_db_session
    await prepare(client, db)
    monkeypatch.setenv("EDGE_V2_HEIGHT_FILL_ENABLED", "false")
    monkeypatch.setenv("EDGE_V2_HEIGHT_MEDIAN_ENABLED", "true")
    get_settings.cache_clear()
    for second in range(6):
        _, snapshot = await frame(client, db, height_context, second, 99 if second == 5 else 50)
    source = monitoring_source("pit-demo")
    assert (await lidar_profiles(db, source, height_context["now"])).profiles[0].average == 100
    assert snapshot.summary.inputSource == "EDGE_STATE" and snapshot.fillObservation is None
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0
    monkeypatch.setenv("EDGE_V2_HEIGHT_MEDIAN_ENABLED", "false")
    get_settings.cache_clear()
    page = await lidar_profiles(db, source, height_context["now"])
    assert page.profiles[0].average == 198
    assert page.profiles[0].valuePolicy == "SPATIAL_BIN_MEDIAN"


def test_height_median_is_opt_in_and_requires_references_and_compact_storage():
    from pydantic import ValidationError

    assert not Settings(_env_file=None).EDGE_V2_HEIGHT_MEDIAN_ENABLED
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EDGE_V2_HEIGHT_MEDIAN_ENABLED=True)
