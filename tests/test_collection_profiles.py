"""Public invented gradual/fast sequences, not field criteria or calibration evidence."""

from datetime import datetime, timedelta

import pytest

from src.app.core.config import Settings, get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.collection_detection import CollectionTracker
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.collection_detection_service import (
    advance,
    collection_height,
    collection_profile,
    collection_scope,
    collection_settle_seconds,
    invalidate,
)
from src.app.services.collection_summary_v2_service import collection_summary
from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
from src.app.services.edge_v2_service import utc
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from tests.test_collection_detection import detector_frame, event_rows
from tests.test_collection_detection import enabled_detector as enabled_detector
from tests.test_height_fill_pipeline import START, frame, prepare
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_height_median import bins_frame
from tests.test_monitoring_v2 import login
from tests.test_sensor_angle_height import angle_context as angle_context
from tests.test_simulation_height import prepare as simulation_prepare


@pytest.fixture
def timing(monkeypatch):
    for key, value in {
        "DROP_METERS": "0.5",
        "STABLE_BAND_METERS": "0.1",
        "BASELINE_SECONDS": "10",
        "CONFIRM_SECONDS": "10",
        "SETTLE_SECONDS": "300",
        "SIMULATION_SETTLE_SECONDS": "20",
    }.items():
        monkeypatch.setenv("EDGE_V2_COLLECTION_" + key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def initial_state():
    return CollectionTracker(pit_id="pit-demo", scope_token="public-toy", observed_at=START)


@pytest.mark.parametrize("interval", [30, 120, 240])
def test_gradual_grabs_keep_one_episode_across_long_pauses(timing, interval):
    state = initial_state()
    grabs = [20 + interval * index for index in range(4)]
    detected, ended = [], []
    for second in range(grabs[-1] + 311):
        height = 800 - 60 * sum(t <= second for t in grabs)
        for kind, detail in advance(state, height, START + timedelta(seconds=second)):
            (detected if kind == "COLLECTION_DETECTED" else ended).append((second, detail))
    assert len(detected) == len(ended) == 1
    assert detected[0][1]["detectionId"] == ended[0][1]["detectionId"]
    assert ended[0][0] == grabs[-1] + 10 + 300
    assert not ended[0][1]["confirmedCompletion"]


def test_fast_simulation_can_end_while_loading_resumes(timing):
    state = initial_state()
    events = []
    for second in range(71):
        height = 800 if second < 20 else 200 if second < 40 else 200 + second - 40
        events.extend(
            (second, kind)
            for kind, _ in advance(
                state, height, START + timedelta(seconds=second), settle_seconds=20
            )
        )
    assert events == [(30, "COLLECTION_DETECTED"), (50, "COLLECTION_SETTLED")]
    assert state.phase == "IDLE"


def test_sub_band_noise_and_short_dip_do_not_extend_quiet_time(timing):
    state = initial_state()
    events = []
    for second in range(51):
        height = 800 if second < 20 else 200 + second % 2
        if second == 45:
            height = 160  # Not sustained for confirmation; not another grab.
        events.extend(
            (second, kind)
            for kind, _ in advance(
                state, height, START + timedelta(seconds=second), settle_seconds=20
            )
        )
    assert events == [(30, "COLLECTION_DETECTED"), (50, "COLLECTION_SETTLED")]


def test_pending_additional_drop_blocks_expiry_and_small_steps_accumulate(timing):
    state = initial_state()
    events = []
    for second in range(90):
        height = 800 if second < 20 else 200
        if second >= 47:
            height -= min(4, second - 46) * 4
        events.extend(
            (second, kind)
            for kind, _ in advance(
                state, height, START + timedelta(seconds=second), settle_seconds=20
            )
        )
    assert events == [(30, "COLLECTION_DETECTED"), (79, "COLLECTION_SETTLED")]


def test_outage_cannot_supply_quiet_time_or_change_episode(timing):
    state = initial_state()
    for second in range(31):
        advance(state, 800 if second < 20 else 200, START + timedelta(seconds=second))
    identity = state.episode_id
    invalidate(state)
    restored = CollectionTracker.model_validate_json(state.model_dump_json())
    events = []
    for second in range(1000, 1301):
        events.extend(
            (second, detail)
            for _, detail in advance(restored, 200, START + timedelta(seconds=second))
        )
    assert len(events) == 1 and events[0][0] == 1300
    assert events[0][1]["detectionId"] == identity


def test_safe_default_does_not_infer_simulation_from_drop_speed():
    defaults = Settings(_env_file=None)
    assert not defaults.EDGE_V2_COLLECTION_DETECTION_ENABLED
    assert defaults.EDGE_V2_COLLECTION_SETTLE_SECONDS == 300
    assert defaults.EDGE_V2_COLLECTION_SIMULATION_SETTLE_SECONDS == 20
    source = MonitoringSource(pit_id="unconfigured", site_id="site-demo", edge_id="edge-demo")
    assert collection_profile(source) == "FIELD"
    assert collection_settle_seconds(source) == 300


async def test_explicit_source_profile_and_scope_only_use_effective_settings(
    client, test_db_session, enabled_detector, monkeypatch
):
    db = test_db_session
    await simulation_prepare(client, db)
    source = monitoring_source("pit-demo")
    pit = await db.get(PitConfig, source.pit_id)
    assert collection_profile(source) == "SIMULATION"
    assert collection_settle_seconds(source) == 3
    scope = collection_scope(source, pit)
    monkeypatch.setenv("EDGE_V2_COLLECTION_SETTLE_SECONDS", "900")
    get_settings.cache_clear()
    assert collection_scope(source, pit) == scope
    monkeypatch.setenv("EDGE_V2_COLLECTION_SIMULATION_SETTLE_SECONDS", "25")
    get_settings.cache_clear()
    assert collection_scope(source, pit) != scope


async def test_upper_height_is_filtered_maximum_not_average_and_requires_current_window(
    client, test_db_session, height_context, monkeypatch
):
    db = test_db_session
    await prepare(client, db)
    source = monitoring_source("pit-demo")
    for second in range(5):
        await bins_frame(client, height_context, second, (0, 100))
    snapshot = await build_snapshot(db, source, height_context["now"])
    assert snapshot.summary.loadPercent == 25
    observation = snapshot.fillObservation
    assert observation is not None
    assert await collection_height(db, source, observation, 50, height_context["now"]) == 100
    # An isolated upper reflection does not replace a full temporal median.
    await bins_frame(client, height_context, 5, (0, 190))
    snapshot = await build_snapshot(db, source, height_context["now"])
    observation = snapshot.fillObservation
    assert observation is not None
    assert await collection_height(db, source, observation, 50, height_context["now"]) == 100
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, "height_window"))
    payload = row.payload
    # A pre-upgrade window has no maximum scalar and must warm up, never borrow the mean.
    for sensor in payload["sensors"].values():
        for old_frame in sensor["frames"]:
            old_frame.pop("maximum_cm")
    row.payload = payload
    assert await collection_height(db, source, observation, 50, height_context["now"]) is None
    monkeypatch.setenv("EDGE_V2_COLLECTION_HEIGHT_BASIS", "REPRESENTATIVE")
    get_settings.cache_clear()
    assert await collection_height(db, source, observation, 50, height_context["now"]) == 50


async def test_unchanged_upper_scrap_does_not_turn_mean_decrease_into_maximum_decrease(
    client, test_db_session, height_context, monkeypatch
):
    monkeypatch.setenv("EDGE_V2_COLLECTION_DROP_METERS", "0.2")
    monkeypatch.setenv("EDGE_V2_COLLECTION_STABLE_BAND_METERS", "0.02")
    monkeypatch.setenv("EDGE_V2_COLLECTION_BASELINE_SECONDS", "2")
    monkeypatch.setenv("EDGE_V2_COLLECTION_CONFIRM_SECONDS", "2")
    monkeypatch.setenv("EDGE_V2_COLLECTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    db = test_db_session
    await prepare(client, db)
    for second in range(24):
        await bins_frame(client, height_context, second, (50 if second < 10 else 0, 100))
        await EdgeV2OperationsWorker().run_once(db, height_context["now"])
    assert await event_rows(db) == []


async def test_worker_exposes_profile_and_quiet_end_through_existing_http_contract(
    client, test_db_session, enabled_detector
):
    db = test_db_session
    await simulation_prepare(client, db)
    await login(client)
    for second in range(40):
        await detector_frame(
            client,
            db,
            enabled_detector,
            second,
            80 if second < 10 else 40 + max(0, second - 25) / 10,
        )
    result = await collection_summary(
        db, monitoring_source("pit-demo"), now=enabled_detector["now"]
    )
    assert result.detectionProfile == "SIMULATION"
    assert result.detectionSettleSeconds == 3
    assert result.detectionHeightBasis == "UPPER"
    assert result.detectionEndPolicy == "NO_FURTHER_DROP"
    rows = await event_rows(db)
    assert [row.kind for row in rows] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert rows[1].payload["detectionProfile"] == "SIMULATION"
    assert rows[1].payload["endPolicy"] == "NO_FURTHER_DROP"
    assert rows[1].payload["heightBasis"] == "UPPER"
    legacy = (await client.get("/api/v1/events?pit_id=pit-demo&category=collection")).json()
    assert legacy["items"][0]["occurredAt"] == result.lastSettledCollectionAt.isoformat().replace(
        "+00:00", "Z"
    )
    assert legacy["items"][0]["confirmedCompletion"] is False


async def test_physical_profile_worker_keeps_four_minute_pause_and_ends_after_long_quiet(
    client, test_db_session, height_context, monkeypatch
):
    # Small public calibrated box. Fast changes do not authorize simulation timing.
    for key, value in {
        "DROP_METERS": "0.2",
        "STABLE_BAND_METERS": "0.02",
        "BASELINE_SECONDS": "2",
        "CONFIRM_SECONDS": "2",
        "SETTLE_SECONDS": "300",
        "SIMULATION_SETTLE_SECONDS": "20",
        "DETECTION_ENABLED": "true",
    }.items():
        monkeypatch.setenv("EDGE_V2_COLLECTION_" + key, value)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            now = height_context["now"]
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.collection_summary_v2_service.datetime", Clock)
    monkeypatch.setattr("src.app.services.collection_ended_service.datetime", Clock)
    get_settings.cache_clear()
    db = test_db_session
    await prepare(client, db)
    await login(client)
    for second in range(560):
        height = 80 if second < 10 else 65 if second < 250 else 50
        await frame(client, db, height_context, second, height, operate=False)
        await EdgeV2OperationsWorker().run_once(db, height_context["now"])
        if second in (200, 500):
            assert [row.kind for row in await event_rows(db)] == ["COLLECTION_DETECTED"]
            assert (await client.get("/api/v1/events?pit_id=pit-demo&category=collection")).json()[
                "items"
            ] == []
    rows = await event_rows(db)
    assert [row.kind for row in rows] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert rows[0].payload["detectionId"] == rows[1].payload["detectionId"]
    assert utc(rows[1].occurred_at) == START + timedelta(seconds=554)
    assert rows[1].payload["detectionProfile"] == "FIELD"
    assert rows[1].payload["settleSeconds"] == 300
    assert rows[1].payload["simulated"] is False
    summary = (await client.get("/api/v2/monitoring/collection-summary?pit_id=pit-demo")).json()
    assert summary["detectionProfile"] == "FIELD" and summary["detectionSettleSeconds"] == 300
    assert summary["completedCollectionsInWindow"] == 0
    legacy = (await client.get("/api/v1/events?pit_id=pit-demo&category=collection")).json()
    assert legacy["items"][0]["occurredAt"] == summary["lastSettledCollectionAt"]
    assert legacy["items"][0]["simulated"] is False
