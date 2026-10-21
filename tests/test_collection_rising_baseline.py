"""Invented rising surfaces exercise baseline preparation without a flat plateau."""

from datetime import UTC, datetime, timedelta

import pytest

from src.app.core.config import get_settings
from src.app.schemas.collection_detection import CollectionTracker
from src.app.services.collection_detection_service import advance, invalidate

START = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def rising_settings(monkeypatch):
    for key, value in {
        "DROP_METERS": "3",
        "STABLE_BAND_METERS": "0.2",
        "BASELINE_SECONDS": "10",
        "CONFIRM_SECONDS": "10",
    }.items():
        monkeypatch.setenv("EDGE_V2_COLLECTION_" + key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("rate", [1, 2, 3, 5])
@pytest.mark.parametrize("settle", [20, 300])
def test_rising_surface_prepares_and_tracks_reference_before_fast_drain(
    rising_settings, rate, settle
):
    state = CollectionTracker(pit_id="public-toy", scope_token="rising", observed_at=START)
    events = []
    peak = 400 + rate * 99
    for second in range(140 + settle):
        height = 400 + rate * second if second < 100 else max(0, peak * (1 - (second - 99) / 12))
        if second == 100:
            assert state.phase == "IDLE"
            assert peak - rate * 20 <= state.baseline_cm <= peak
        events.extend(
            (second, kind, detail)
            for kind, detail in advance(state, height, START + timedelta(seconds=second), settle)
        )
    assert [kind for _, kind, _ in events] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert events[1][0] - events[0][0] >= settle
    assert events[0][2]["detectionId"] == events[1][2]["detectionId"]


def test_brief_upper_spike_cannot_become_a_precollection_reference(rising_settings):
    state = CollectionTracker(pit_id="public-toy", scope_token="spike", observed_at=START)
    events = []
    for second in range(120):
        height = 400 if second < 30 or second >= 36 else 1000
        events.extend(advance(state, height, START + timedelta(seconds=second), 20))
    assert events == []
    assert state.baseline_cm == 400


def test_interruption_discards_unconfirmed_rising_reference(rising_settings):
    state = CollectionTracker(pit_id="public-toy", scope_token="gap", observed_at=START)
    for second in range(9):
        advance(state, 700 + second * 4, START + timedelta(seconds=second), 20)
    invalidate(state)
    state = CollectionTracker.model_validate_json(state.model_dump_json())
    for second in range(100, 110):
        assert advance(state, 100, START + timedelta(seconds=second), 20) == []
        assert state.phase == "BASELINING"
    assert advance(state, 100, START + timedelta(seconds=110), 20) == []
    assert state.phase == "IDLE" and state.baseline_cm == 100
