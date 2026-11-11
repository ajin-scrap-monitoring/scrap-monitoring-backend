"""Synthetic scan means: UI-hold eligibility and hard timestamp failures stay distinct."""

from datetime import timedelta

import pytest

from src.app.services.height_median_service import (
    HeightFrame,
    HeightWindow,
    SensorWindow,
    filter_scan_mean,
)
from tests.test_height_fill_pipeline import START
from tests.test_sensor_angle_height import calculate


@pytest.mark.parametrize(
    "case,expected",
    [
        ("warming", "WARMING"),
        ("zero_height", "WARMING"),
        ("missing_current", "MISSING"),
        ("missing_time", "MISSING"),
        ("missing_window", "MISSING"),
        ("missing_sensor", "MISSING"),
        ("empty_window", "MISSING"),
        ("wrong_basis", "MISSING"),
        ("different_scan", "MISSING"),
        ("missing_history_value", "MISSING"),
        ("wrong_history_shape", "MISSING"),
        ("missing_depth", "MISSING"),
        ("ready", None),
    ],
)
def test_only_matching_valid_incomplete_scan_window_authorizes_display_hold(case, expected):
    profile = calculate(height=0 if case == "zero_height" else 50)
    profile.measuredAt = START
    history = SensorWindow(
        basis="RECEIVED_POINTS",
        positions=[0.0],
        frames=[HeightFrame(at=START, heights=[profile.average])],
    )
    window = HeightWindow(
        scope="synthetic", config_revision="cfg-1", sensors={profile.sensorId: history}
    )
    depth = 100.0
    if case == "missing_current":
        profile.average = None
        for sample in profile.samples:
            sample.update(height=None, valid=False)
    elif case == "missing_time":
        profile.measuredAt = None
    elif case == "missing_window":
        window = None
    elif case == "missing_sensor":
        window.sensors = {}
    elif case == "empty_window":
        history.frames = []
    elif case == "wrong_basis":
        history.basis = "SPATIAL_BINS"
    elif case == "different_scan":
        history.frames[-1].at = START - timedelta(seconds=1)
    elif case == "missing_history_value":
        history.frames[-1].heights = [None]
    elif case == "wrong_history_shape":
        history.frames[-1].heights = [50, 50]
    elif case == "missing_depth":
        depth = None
    elif case == "ready":
        history.frames = [
            HeightFrame(at=START - timedelta(seconds=4 - i), heights=[50]) for i in range(5)
        ]
    before = [dict(sample) for sample in profile.samples]
    filter_scan_mean(profile, window, 5, depth)
    assert profile.samples == before
    detailed = {
        r
        for r in profile.reasonCodes
        if r in {"HEIGHT_TEMPORAL_WARMING", "HEIGHT_TEMPORAL_MISSING"}
    }
    assert detailed == ({f"HEIGHT_TEMPORAL_{expected}"} if expected else set())
    assert ("HEIGHT_TEMPORAL_WARMING_OR_MISSING" in profile.reasonCodes) == (expected == "WARMING")
    assert profile.filteredAverage == (50 if case == "ready" else None)
    assert profile.sectionFillPercent == (50 if case == "ready" else None)
