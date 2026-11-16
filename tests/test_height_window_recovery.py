"""Recover from one absent observation using real recent samples, never a held result."""

from datetime import timedelta

import pytest

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from tests.test_edge_world_height import world_context as world_context
from tests.test_filtered_lidar_profiles import cloud_context as cloud_context
from tests.test_filtered_lidar_profiles import frame as cloud_frame
from tests.test_filtered_lidar_profiles import prepare as cloud_prepare
from tests.test_height_fill_pipeline import START, frame, prepare
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_sensor_angle_height import angle_context as angle_context


@pytest.mark.parametrize("old_format", [False, True])
async def test_fresh_scan_after_one_missing_second_recomputes_without_full_warmup(
    client, test_db_session, height_context, monkeypatch, old_format
):
    db = test_db_session
    monkeypatch.setattr(get_settings(), "EDGE_V2_ASSESSMENT_TTL_SECONDS", 1)
    await prepare(client, db)
    for second, percent in enumerate([10, 20, 30, 40, 50]):
        _, before = await frame(client, db, height_context, second, percent)
    assert before.summary.loadPercent == pytest.approx(30)
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    if old_format:
        payload = window.payload
        window.payload = {
            **payload,
            "sensors": {
                key: {k: v for k, v in value.items() if k != "full_window_seen"}
                for key, value in payload["sensors"].items()
            },
        }
        await db.commit()
    # No scan at second 5. The current scan is 90%, and recent real values are
    # 30/40/50/90. Their median is 45%, not the old result 30% or current raw 90%.
    _, result = await frame(client, db, height_context, 6, 90)
    assert result.dataState == "FRESH" and result.summary.filterState == "READY"
    assert result.summary.rawLoadPercent == pytest.approx(90)
    assert result.summary.loadPercent == pytest.approx(45)
    source = monitoring_source("pit-demo")
    page = await lidar_profiles(db, source, height_context["now"])
    assert page.meanSectionFillPercent == pytest.approx(45)
    assert all("HEIGHT_TEMPORAL_SINGLE_SLOT_MISSING" in p.reasonCodes for p in page.profiles)
    await db.refresh(window)
    for sensor in window.payload["sensors"].values():
        assert len(sensor["frames"]) == 4 and sensor["full_window_seen"]
    # The temporal history never extends the independently configured current-data TTL.
    expired = await build_snapshot(db, source, START + timedelta(seconds=7))
    assert expired.dataState == "STALE" and expired.summary.loadPercent is None


@pytest.mark.parametrize("next_second", [7, 10])
async def test_more_missing_slots_or_long_gap_do_not_authorize_a_value(
    client, test_db_session, height_context, next_second
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    _, result = await frame(client, db, height_context, next_second, 90)
    assert result.summary.loadPercent is None
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert all(len(s["frames"]) < 4 for s in window.payload["sensors"].values())
    if next_second == 10:
        assert all(not s["full_window_seen"] for s in window.payload["sensors"].values())


async def test_invalid_current_scan_does_not_borrow_prepared_history(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    _, invalid = await frame(client, db, height_context, 5, 90, valid=False)
    assert invalid.summary.rawLoadPercent is None and invalid.summary.loadPercent is None
    for second in range(6, 11):
        _, result = await frame(client, db, height_context, second, 90)
        assert (result.summary.loadPercent is not None) == (second == 10)


async def test_gap_restarts_critical_confirmation_but_has_no_second_warmup(
    client, test_db_session, height_context, monkeypatch
):
    from sqlalchemy import func, select

    db = test_db_session
    monkeypatch.setattr(get_settings(), "EDGE_V2_CRITICAL_CONFIRM_SECONDS", 3)
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    for second in range(5, 8):
        _, before = await frame(client, db, height_context, second, 95)
    assert before.summary.loadPercent == pytest.approx(95)
    assert before.operations.effectiveState == "WARNING"
    # Second 8 is absent. Confirm only from the new continuous run starting at 9.
    for second in range(9, 13):
        _, after = await frame(client, db, height_context, second, 95)
        assert after.summary.filterState == "READY"
        assert after.operations.effectiveState == ("CRITICAL" if second == 12 else "WARNING")
        assert await db.scalar(select(func.count()).select_from(AlertLog)) == int(second == 12)


async def test_state_graph_recovers_independently_from_measurement_fill(
    client, test_db_session, cloud_context
):
    db = test_db_session
    await cloud_prepare(client, db)
    for second, height in enumerate([10, 20, 30, 40, 50]):
        _, page = await cloud_frame(client, db, cloud_context, second, height)
    assert page.profiles[0].filteredAverage == pytest.approx(30)
    _, page = await cloud_frame(client, db, cloud_context, 6, 90)
    assert page.profiles[0].average == pytest.approx(90)
    assert page.profiles[0].filteredAverage == pytest.approx(45)
    assert page.meanSectionFillPercent == pytest.approx(45)
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), cloud_context["now"])
    assert snapshot.summary.loadPercent == pytest.approx(20, abs=0.05)
