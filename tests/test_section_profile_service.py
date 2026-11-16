"""Synthetic HTTP inputs keep bin curves and one selected fusion history source consistent."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Artifact
from src.app.models.log import AlertLog
from src.app.schemas.edge_v2 import Assessment
from src.app.services.edge_v2_operations import process_source
from src.app.services.edge_v2_service import digest
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from src.app.services.section_profile_service import scan_anchors
from tests.test_edge_world_height import REFERENCE, world_inputs
from tests.test_height_fill_pipeline import START, send
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_monitoring_v2 import login, status
from tests.test_section_profile_context import inputs


@pytest.fixture
def section_context(height_context, monkeypatch):
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", "[]")
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    monkeypatch.setenv("EDGE_V2_SECTION_PROFILES_ENABLED", "true")
    get_settings.cache_clear()
    yield height_context
    get_settings.cache_clear()


async def prepare(client, db, limited=False):
    db.add(
        PitConfig(
            id="pit-demo",
            name="Synthetic",
            total_depth_cm=100,
            warning_threshold_percent=75,
            critical_threshold_percent=90,
        )
    )
    await db.commit()
    _, c, *_ = inputs()
    c = c.model_dump(mode="json")
    if not limited:
        c["judgment"]["verification_state"] = "VERIFIED"
        c["judgment"]["fusion"]["verification_state"] = "VERIFIED"
    assert (await send(client, c)).status_code == 201


async def frame(client, db, clock, second, percent=50, limited=False, bad_pair=False):
    delta_ms = 101 if bad_pair else 1
    observed = START + timedelta(seconds=second, milliseconds=delta_ms)
    now = observed + timedelta(milliseconds=1)
    clock["now"] = now
    s = status(f"section-status-{second}", now, second + 2)
    s["config_revision"] = "cfg-1"
    assert (await send(client, s)).status_code == 201
    _, m = world_inputs()
    m.update(
        message_id=f"section-measurement-{second}",
        generated_at=now.isoformat(),
        pair_delta_ms=float(delta_ms),
        pair_state="UNMATCHED" if bad_pair else "MATCHED",
    )
    a, *_ = inputs()
    a = a.model_dump(mode="json")
    a.update(
        message_id=f"section-state-{second}",
        assessment_id=f"section-assessment-{second}",
        generated_at=now.isoformat(),
        observed_from=(START + timedelta(seconds=second)).isoformat(),
        observed_to=observed.isoformat(),
    )
    for index, sensor in enumerate(m["sensors"]):
        sid = sensor["sensor_id"]
        scan = sensor["scan"]
        scan_id = f"section-scan-{index}-{second}"
        scan.update(
            scan_id=scan_id,
            sequence=second,
            clock_domain_id="synthetic-boot",
            acquired_at=(
                START + timedelta(seconds=second, milliseconds=delta_ms if index else 0)
            ).isoformat(),
            acquired_monotonic_ns=str((second + 1) * 10**9 + (delta_ms * 10**6 if index else 0)),
        )
        evidence = next(e for e in a["evidence"]["scans"] if e["sensor_id"] == sid)
        evidence["scan_id"] = scan_id
        component = next(c for c in a["fusion_result"]["components"] if c["sensor_id"] == sid)
        component.update(
            section_ratio=percent / 100, calibrated_ratio=percent / 100, anchor_scan_id=scan_id
        )
        section = next(s for s in a["section_profiles"] if s["sensor_id"] == sid)
        for b in section["profile"]["bins"]:
            b["height_mm"] = float(percent)
        section["profile"].update(area_mm2=float(percent) * 2000, area_ratio=percent / 100)
        if not limited:
            section.update(state="AVAILABLE", reason_codes=[])
    a["fusion_result"].update(ratio=percent / 100, pair_delta_ms=float(delta_ms))
    a["fill_estimate"]["ratio"] = percent / 100
    if not limited:
        a.update(decision_state="AVAILABLE", reason_codes=[])
    if bad_pair:
        a.update(
            fusion_result=None,
            fill_estimate=None,
            fill_state="UNAVAILABLE",
            decision_state="UNAVAILABLE",
            reason_codes=["SENSOR_TIME_MISMATCH"],
        )
    response = await send(client, m)
    assert response.status_code == 201, response.text
    response = await send(client, a)
    assert response.status_code == 201, response.text
    source = monitoring_source("pit-demo")
    await process_source(db, source, now)
    return await build_snapshot(db, source, now), await lidar_profiles(db, source, now)


async def test_curves_raw_fusion_and_five_second_display_share_one_input(
    client, test_db_session, section_context
):
    db = test_db_session
    await prepare(client, db)
    for second, percent in enumerate([10, 20, 30, 40, 50]):
        snap, page = await frame(client, db, section_context, second, percent)
        if second < 4:
            assert snap.summary.loadPercent is None
    assert snap.dataState == "FRESH" and snap.summary.filterState == "READY"
    assert snap.summary.inputSource == "EDGE_STATE"
    assert snap.summary.loadPercent == page.meanSectionFillPercent == 30
    assert snap.summary.rawLoadPercent == page.rawMeanSectionFillPercent == 50
    assert all(p.sectionFillPercent == 50 for p in page.profiles)
    assert all(p.referenceDepthCm == 10 for p in page.profiles)
    history = await load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(minutes=1),
        now=START + timedelta(minutes=1),
    )
    values = [s.valuePercent for s in history.samples if s.valuePercent is not None]
    assert values == [30]
    await login(client)
    response = await client.get("/api/v2/monitoring/profiles", params={"pit_id": "pit-demo"})
    assert response.status_code == 200
    for p in response.json()["profiles"]:
        assert "sampleSource" not in p and p["sectionSource"] == "EDGE_SECTION_PROFILE"
        assert p["sectionJudgmentState"] == "AVAILABLE"
        assert len(p["samples"]) == 2
        assert [s["positionRatio"] for s in p["samples"]] == [0.25, 0.75]
        assert all(s["binSource"] == "OBSERVED" for s in p["samples"])


async def test_limited_values_are_explicit_references_and_never_create_operational_alerts(
    client, test_db_session, section_context
):
    db = test_db_session
    await prepare(client, db, limited=True)
    for second in range(9):
        snap, page = await frame(client, db, section_context, second, 95, limited=True)
    assert snap.dataState == "UNAVAILABLE"
    assert snap.summary.loadPercent is page.meanSectionFillPercent is None
    assert page.rawMeanSectionFillPercent == 95
    assert all(p.sectionJudgmentState == "LIMITED" for p in page.profiles)
    assert "DEFAULT_SETTINGS" in snap.reasonCodes
    assert "SECTION_JUDGMENT_LIMITED" in snap.reasonCodes
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0


async def test_single_unmatched_pair_does_not_reuse_current_value_or_restart_prepared_history(
    client, test_db_session, section_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(6):
        snap, _ = await frame(client, db, section_context, second, 95)
    snap, page = await frame(client, db, section_context, 6, 95, bad_pair=True)
    assert snap.summary.loadPercent is page.meanSectionFillPercent is None
    assert "SECTION_PAIR_UNMATCHED" in snap.reasonCodes
    snap, _ = await frame(client, db, section_context, 7, 95)
    assert snap.summary.filterState == "READY"
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0
    for second in (8, 9):
        await frame(client, db, section_context, second, 95)
        assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0
    await frame(client, db, section_context, 10, 95)
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 1


async def test_stale_section_input_does_not_fall_back_to_legacy_height_flags(
    client, test_db_session, section_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, section_context, second)
    when = section_context["now"] + timedelta(seconds=30)
    source = monitoring_source("pit-demo")
    snap = await build_snapshot(db, source, when)
    page = await lidar_profiles(db, source, when)
    assert get_settings().EDGE_V2_HEIGHT_FILL_ENABLED
    assert snap.summary.loadPercent is page.meanSectionFillPercent is None
    assert all(p.samples == [] for p in page.profiles)


async def test_long_gap_requires_a_new_full_window(client, test_db_session, section_context):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, section_context, second)
    for second in range(20, 24):
        snap, page = await frame(client, db, section_context, second)
        assert snap.summary.filterState == "WARMING"
        assert snap.summary.loadPercent is page.meanSectionFillPercent is None
    snap, page = await frame(client, db, section_context, 24)
    assert snap.summary.loadPercent == page.meanSectionFillPercent == 50


async def test_cloud_timing_without_raw_points_is_usable_but_conflicting_scan_metadata_is_not(
    test_db_session,
):
    from tests.test_filtered_lidar_profiles import inputs as cloud_inputs

    _, raw = cloud_inputs()
    assessment = Assessment.model_validate(raw)
    db = test_db_session
    anchors = await scan_anchors(db, assessment, ("lidar-a", "lidar-b"))
    assert set(anchors) == {"lidar-a", "lidar-b"}
    metadata = anchors["lidar-a"].model_dump(mode="json")
    metadata["acquired_at"] = (
        anchors["lidar-a"].acquired_at + timedelta(milliseconds=1)
    ).isoformat()
    db.add(
        EdgeV2Artifact(
            site_id=assessment.site_id,
            edge_id=assessment.edge_id,
            kind="scan_source",
            key_hash=digest([anchors["lidar-a"].scan_id]),
            artifact_key=[anchors["lidar-a"].scan_id],
            payload=metadata,
        )
    )
    await db.commit()
    conflicting = await scan_anchors(db, assessment, ("lidar-a", "lidar-b"))
    assert set(conflicting) == {"lidar-b"}


async def test_sse_uses_the_same_selected_fusion_median(
    client, test_db_session, section_context, monkeypatch
):
    import src.app.api.monitoring_v2 as route
    from tests.test_monitoring_v2 import ConnectedRequest

    db = test_db_session
    await prepare(client, db)
    for second, percent in enumerate([10, 20, 30, 40, 50]):
        await frame(client, db, section_context, second, percent)
    real_sleep = asyncio.sleep

    async def immediate_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(route.asyncio, "sleep", immediate_sleep)
    session_id, session = await login(client)
    response = await route.events(
        ConnectedRequest(), session, monitoring_source("pit-demo"), db, session_id
    )
    generator = response.body_iterator
    try:
        initial = await anext(generator)
        payload = json.loads(initial.split("data: ", 1)[1])
        assert payload["summary"]["loadPercent"] == 30
        await frame(client, db, section_context, 5, 80)
        changed = await anext(generator)
        payload = json.loads(changed.split("data: ", 1)[1])
        assert payload["summary"]["loadPercent"] == 40
        assert payload["summary"]["rawLoadPercent"] == 80
    finally:
        await generator.aclose()
