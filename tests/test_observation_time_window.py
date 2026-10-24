"""Independent synthetic clocks exercise observation windows across UTC boundaries."""

from datetime import timedelta
from statistics import median

import pytest

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest
from src.app.schemas.collection_detection import CollectionTracker
from src.app.schemas.monitoring_v2 import LidarProfileV2
from src.app.services.collection_detection_service import advance as detect_collection
from src.app.services.collection_detection_service import invalidate
from src.app.services.height_median_service import SensorWindow, advance
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from tests.test_height_fill_pipeline import START, prepare
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_height_median import bins_frame


def profile(second, basis="RECEIVED_POINTS"):
    return LidarProfileV2(
        sensorId="synthetic-clock",
        referenceDistanceCm=1000,
        measuredAt=START + timedelta(seconds=second),
        calibrationState="SIMULATION",
        coverageBasis=basis,
        average=500,
        maximum=500,
        samples=[{"positionRatio": 0.5, "height": 500, "valid": True}],
    )


@pytest.mark.parametrize("basis", ["RECEIVED_POINTS", "SPATIAL_BINS"])
def test_prepared_height_window_survives_distinct_scans_in_one_utc_second(basis):
    window = SensorWindow()
    for index in range(20):
        # Adjacent scans remain about one second apart; one moves three milliseconds.
        second = index + 0.999 + (0.003 if index == 8 else 0)
        window = advance(profile(second, basis), window)
        if index >= 4:
            assert len(window.frames) == 5


@pytest.mark.parametrize("phase", [0, 0.499, 0.999])
def test_small_continuous_clock_drift_never_restarts_prepared_window(phase):
    window = SensorWindow()
    for index in range(1200):
        window = advance(profile(phase + index * 1.001), window)
        if index >= 4:
            assert len(window.frames) == 5


def test_dense_stream_is_bounded_and_does_not_prepare_a_window_in_one_second():
    window = SensorWindow()
    for index in range(100):
        window = advance(profile(index / 100), window)
        assert len(window.frames) < 5
    # Faster messages still form a bounded time window once enough time passes.
    for index in range(100, 3000):
        window = advance(profile(index / 100), window)
        assert len(window.frames) <= 5
        if index >= 500:
            assert len(window.frames) == 5


def test_window_serialization_keeps_clock_anchor_but_legacy_state_can_recover():
    window = SensorWindow()
    for index in range(8):
        window = advance(profile(index + 0.999), window)
    restored = SensorWindow.model_validate_json(window.model_dump_json())
    for second in [9.002, 9.999, 10.999]:
        restored = advance(profile(second), restored)
        assert len(restored.frames) == 5
    legacy = restored.model_dump(mode="json")
    legacy.pop("slot_anchor_at")
    recovered = advance(profile(11.999), SensorWindow.model_validate(legacy))
    assert len(recovered.frames) == 5


@pytest.fixture
def collection_timing(monkeypatch):
    for key, value in {
        "DROP_METERS": "3",
        "STABLE_BAND_METERS": ".2",
        "BASELINE_SECONDS": "10",
        "CONFIRM_SECONDS": "10",
    }.items():
        monkeypatch.setenv("EDGE_V2_COLLECTION_" + key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("initial_percent", [85, 90])
@pytest.mark.parametrize("drain_seconds", [10, 15])
def test_fast_drop_and_settlement_survive_clock_collision(
    collection_timing, initial_percent, drain_seconds
):
    window = SensorWindow()
    state = CollectionTracker(pit_id="public-toy", scope_token="toy", observed_at=START)
    events = []
    for index in range(100):
        second = index + 0.999 + (0.003 if index == 27 else 0)
        current = profile(second)
        # Independent invented 1000cm range, unrelated to a site's measurements.
        remaining = 1 if index < 20 else max(0, 1 - (index - 19) / drain_seconds)
        height = initial_percent * 10 * remaining
        current.average = current.maximum = height
        current.samples[0]["height"] = height
        window = advance(current, window)
        window = SensorWindow.model_validate_json(window.model_dump_json())
        if len(window.frames) != 5:
            invalidate(state)
            continue
        upper = float(median(frame.maximum_cm for frame in window.frames))
        events.extend(detect_collection(state, upper, current.measuredAt, settle_seconds=20))
    assert [kind for kind, _ in events] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert events[0][1]["detectionId"] == events[1][1]["detectionId"]
    assert events[0][1]["dropCm"] >= 300
    assert all(detail["confirmedCompletion"] is False for _, detail in events)


async def test_receiver_keeps_height_and_percentage_windows_ready_at_utc_boundary(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    source = monitoring_source("pit-demo")
    for index in range(20):
        second = index + 0.999 + (0.003 if index == 8 else 0)
        await bins_frame(client, height_context, index, (100, 100), second=second)
        if index >= 4:
            window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
            assert all(len(s["frames"]) == 5 for s in window.payload["sensors"].values())
            filtered = await db.get(EdgeV2Filter, ("site-demo", "edge-demo"))
            assert filtered.payload["state"] == "READY"
            snapshot = await build_snapshot(db, source, height_context["now"])
            assert snapshot.dataState == "FRESH"
            assert snapshot.summary.loadPercent == 50
