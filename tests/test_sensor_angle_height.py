"""Received-angle regression cases using an invented box, never deployed sensor data."""

import copy
import json
import math
from datetime import timedelta

import pytest

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.lidar_height import MAX_HEIGHT_SAMPLES, SimulationHeightReference
from src.app.services.height_fill_service import height_method
from src.app.services.height_median_service import SensorWindow, advance
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import lidar_profiles, monitoring_source
from tests import test_simulation_height as legacy
from tests.test_height_fill_pipeline import START, send
from tests.test_height_fill_pipeline import height_context as height_context

REFERENCE = {k: v for k, v in legacy.REFERENCE.items() if k != "sampling_policy"}
ORIGINAL_INPUTS = legacy.raw_inputs


def angle_inputs(height=50.0, count=6):
    c, m = ORIGINAL_INPUTS(height)
    for sensor in m["sensors"]:
        template = sensor["scan"]["points"][0]
        points = []
        for index in range(count):
            angle = round(index * 50000 / max(1, count - 1))
            distance = round((100 - height) / 0.8 / math.cos(math.radians(angle / 1000)) * 10)
            points.append(
                {**template, "index": index, "angle_mdeg": angle, "distance_mm": distance}
            )
        sensor["scan"].update(source_point_count=count, selected_point_count=count, points=points)
    return c, m


def calculate(height=50.0, count=6, m=None, reference=None):
    c, raw = angle_inputs(height, count)
    return legacy.calculate(c=c, m=m or raw, reference=reference or REFERENCE)


def test_default_uses_every_received_angle_without_spatial_bins():
    assert SimulationHeightReference.model_validate(REFERENCE).sampling_policy == "SENSOR_ANGLE"
    c, m = angle_inputs(90)
    original = copy.deepcopy(m)
    result = legacy.calculate(c=c, m=m, reference=REFERENCE)
    assert m == original
    assert result.valuePolicy == "SENSOR_ANGLE" and result.coverageBasis == "RECEIVED_POINTS"
    assert len(result.samples) == 6  # All six points occupy one legacy spatial bin.
    assert [s["angleMdeg"] for s in result.samples] == [0, 10000, 20000, 30000, 40000, 50000]
    assert result.coverageRatio == 1
    assert result.sectionFillPercent == pytest.approx(90, abs=0.04)
    assert legacy.calculate(c=c, m=m, reference=legacy.REFERENCE).sectionFillPercent is None
    for bins in (2, 20, 200):
        assert calculate(90, reference={**REFERENCE, "bin_count": bins}).samples == result.samples


@pytest.mark.parametrize("height", [25, 75, 90, 95, 99])
def test_fixed_sensor_angles_remain_available_from_empty_to_nearly_full(height):
    result = calculate(height)
    assert result.sectionFillPercent == pytest.approx(height, abs=0.05)
    assert all(s["valid"] for s in result.samples)


def test_exact_floor_zero_is_valid_without_rounding_negative_ranges_into_zero():
    result = calculate(0, count=1)
    assert result.sectionFillPercent == 0
    assert result.samples[0]["height"] == 0 and result.samples[0]["valid"]


def test_actual_received_count_can_exceed_old_200_bin_limit():
    result = calculate(90, count=800)
    assert len(result.samples) == 800
    assert result.sectionFillPercent == pytest.approx(90, abs=0.04)


def test_invalid_received_measurements_stay_gaps_and_count_against_valid_fraction():
    _, m = angle_inputs()
    m["sensors"][0]["scan"]["points"][2]["distance_mm"] = 0
    result = calculate(m=m)
    assert len(result.samples) == 6
    assert result.samples[2]["height"] is None and not result.samples[2]["valid"]
    assert result.samples[2]["positionRatio"] is None
    assert result.coverageRatio == pytest.approx(5 / 6)
    assert result.sectionFillPercent == pytest.approx(50, abs=0.04)
    for p in m["sensors"][0]["scan"]["points"][:5]:
        p["distance_mm"] = 0
    for threshold in (0.7, 1.0):
        result = calculate(m=m, reference={**REFERENCE, "minimum_coverage": threshold})
        assert result.coverageRatio == pytest.approx(1 / 6)
        assert result.sectionFillPercent == pytest.approx(50, abs=0.04)
    m["sensors"][0]["scan"]["points"][-1]["distance_mm"] = 0
    result = calculate(m=m)
    assert result.sectionFillPercent is None
    assert "HEIGHT_NO_VALID_POINTS" in result.reasonCodes


def test_duplicate_angles_keep_distinct_source_indices_and_invalid_gaps():
    _, m = angle_inputs()
    m["sensors"][0]["scan"]["points"][1]["angle_mdeg"] = 0
    m["sensors"][0]["scan"]["points"][1]["distance_mm"] = 0
    result = calculate(m=m)
    assert len(result.samples) == 6
    assert result.samples[0]["angleMdeg"] == result.samples[1]["angleMdeg"] == 0
    assert result.samples[0]["sourceIndex"] != result.samples[1]["sourceIndex"]
    assert result.samples[0]["valid"] and not result.samples[1]["valid"]


def test_oversized_scan_is_rejected_without_truncating():
    result = calculate(count=MAX_HEIGHT_SAMPLES + 1)
    assert result.calibrationState == "UNAVAILABLE" and result.samples == []
    assert "HEIGHT_SAMPLE_LIMIT_EXCEEDED" in result.reasonCodes


def test_scan_mean_window_accepts_changing_received_points_and_does_not_count_repeated_scans():
    profile = calculate(50)
    first = advance(profile, SensorWindow())
    later = calculate(90)
    later.measuredAt = START + timedelta(seconds=1)
    assert later.samples[1]["positionRatio"] != profile.samples[1]["positionRatio"]
    continued = advance(later, first)
    assert len(continued.frames) == 2
    changed = later.model_copy(deep=True)
    changed.measuredAt = START + timedelta(seconds=2)
    changed.samples[0]["sourceIndex"] = 900
    assert len(advance(changed, continued).frames) == 3
    changed.samples[0]["sourceIndex"] = later.samples[0]["sourceIndex"]
    changed.samples[0]["angleMdeg"] = 1
    assert len(advance(changed, continued).frames) == 3
    assert len(advance(later, continued).frames) == 2  # Repeated scan does not advance.
    later.measuredAt = START + timedelta(seconds=5)
    assert len(advance(later, continued).frames) == 1


@pytest.fixture
def angle_context(height_context, monkeypatch):
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", "[]")
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    monkeypatch.setenv("EDGE_V2_HEIGHT_MEDIAN_ENABLED", "true")
    monkeypatch.setattr(legacy, "raw_inputs", angle_inputs)
    get_settings.cache_clear()
    yield height_context
    get_settings.cache_clear()


async def test_angle_median_spike_history_and_current_missing_measurement(
    client, test_db_session, angle_context
):
    db = test_db_session
    await legacy.prepare(client, db)
    for second in range(12):
        _, snapshot = await legacy.frame(
            client, db, angle_context, second, 95 if second == 7 else 50
        )
        if second < 4:
            assert snapshot.summary.loadPercent is None
        else:
            assert snapshot.summary.loadPercent == pytest.approx(50, abs=0.04)
        page = await lidar_profiles(db, monitoring_source("pit-demo"), angle_context["now"])
        assert all(any(s["valid"] for s in p.samples) for p in page.profiles)
        if second == 7:
            assert page.profiles[0].average == pytest.approx(95, abs=0.04)
            assert page.profiles[0].filteredAverage == pytest.approx(50, abs=0.04)
    source = monitoring_source("pit-demo")
    page = await lidar_profiles(db, source, angle_context["now"])
    assert page.meanSectionFillPercent == pytest.approx(50, abs=0.04)
    for p in page.profiles:
        assert p.valuePolicy == "SENSOR_ANGLE"
        assert p.summaryValuePolicy == "SCAN_MEAN_TEMPORAL_MEDIAN"
        assert len(p.samples) == 6 and p.coverageRatio == 1
    assert height_method(source, 200).startswith("SIMULATION_SENSOR_ANGLE_V1:")
    history = await load_history(db, source, START, START + timedelta(seconds=12), 60)
    sample = history.samples[0]
    assert sample.simulated and sample.methodVersion.startswith("SIMULATION_SENSOR_ANGLE_V1:")
    assert sample.averagePercent == pytest.approx(50, abs=0.04)
    assert sample.validSamples == 8 and sample.invalidSamples == 4
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert all(len(s["frames"]) == 5 for s in window.payload["sensors"].values())
    # Current invalid input is never filled with an older height, even after warming.
    now = START + timedelta(seconds=12)
    angle_context["now"] = now
    _, m = angle_inputs()
    m.update(message_id="missing-angle-value", generated_at=now.isoformat())
    for index, sensor in enumerate(m["sensors"]):
        sensor["scan"].update(
            scan_id=f"new-scan-{index}",
            sequence=12,
            acquired_at=now.isoformat(),
            acquired_monotonic_ns=str(13 * 10**9),
        )
        sensor["scan"]["points"][0]["distance_mm"] = 0
    assert (await send(client, m)).status_code == 201
    page = await lidar_profiles(db, source, now)
    assert all(p.samples[0]["height"] is None for p in page.profiles)
    assert page.meanSectionFillPercent == pytest.approx(50, abs=0.04)


async def test_received_800_points_have_bounded_temporal_storage_and_api(
    client, test_db_session, angle_context, monkeypatch
):
    monkeypatch.setattr(legacy, "raw_inputs", lambda height=50: angle_inputs(height, 800))
    db = test_db_session
    await legacy.prepare(client, db)
    for second in range(5):
        payload, snapshot = await legacy.frame(client, db, angle_context, second, 90)
    assert snapshot.summary.loadPercent == pytest.approx(90, abs=0.04)
    assert (await send(client, payload)).json()["duplicate"]
    await legacy.login(client)
    response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
    assert response.status_code == 200
    assert all(len(p["samples"]) == 800 for p in response.json()["profiles"])
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert all(len(s["frames"]) == 5 for s in window.payload["sensors"].values())
    assert all(
        len(f["heights"]) == 1 for s in window.payload["sensors"].values() for f in s["frames"]
    )
    assert "position_mm" not in json.dumps(window.payload)


async def test_switching_angle_and_legacy_methods_resets_window_and_keeps_history_mixed(
    client, test_db_session, angle_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    for second in range(5):
        _, snapshot = await legacy.frame(client, db, angle_context, second)
    assert snapshot.summary.loadPercent == pytest.approx(50, abs=0.04)
    monkeypatch.setenv(
        "EDGE_V2_SIMULATION_HEIGHT_REFERENCES",
        json.dumps([{**legacy.REFERENCE, "minimum_coverage": 0.5}]),
    )
    get_settings.cache_clear()
    for second in range(5, 10):
        _, snapshot = await legacy.frame(client, db, angle_context, second)
        assert (snapshot.summary.loadPercent is not None) == (second == 9)
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=10), 60
    )
    sample = history.samples[0]
    assert sample.inputSource == "MIXED" and sample.valuePercent is None and sample.simulated


async def test_variable_received_count_and_one_valid_point_do_not_block_height_or_fill(
    client, test_db_session, angle_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    for second, count in enumerate((6, 9, 1, 800, 13)):
        monkeypatch.setattr(
            legacy, "raw_inputs", lambda height=50, n=count: angle_inputs(height, n)
        )
        _, snapshot = await legacy.frame(client, db, angle_context, second, 90)
        page = await lidar_profiles(db, monitoring_source("pit-demo"), angle_context["now"])
        assert all(len(p.samples) == count for p in page.profiles)
    assert snapshot.summary.loadPercent == pytest.approx(90, abs=0.04)


async def test_no_valid_current_height_sensor_failure_staleness_and_reference_change(
    client, test_db_session, angle_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    source = monitoring_source("pit-demo")
    for second in range(5):
        await legacy.frame(client, db, angle_context, second)

    def invalid_inputs(height=50):
        c, m = angle_inputs(height)
        for sensor in m["sensors"]:
            for point in sensor["scan"]["points"]:
                point["distance_mm"] = 0
        return c, m

    with monkeypatch.context() as temporary:
        temporary.setattr(legacy, "raw_inputs", invalid_inputs)
        _, snapshot = await legacy.frame(client, db, angle_context, 5)
    assert snapshot.summary.loadPercent is None and snapshot.dataState == "UNAVAILABLE"
    page = await lidar_profiles(db, source, angle_context["now"])
    assert all(not s["valid"] and s["height"] is None for p in page.profiles for s in p.samples)
    for second in range(6, 11):
        _, snapshot = await legacy.frame(client, db, angle_context, second)
        assert (snapshot.summary.loadPercent is not None) == (second == 10)
    _, snapshot = await legacy.frame(client, db, angle_context, 11, sensor_failure=True)
    assert snapshot.summary.loadPercent is None and snapshot.dataState == "UNAVAILABLE"
    for second in range(12, 17):
        _, snapshot = await legacy.frame(client, db, angle_context, second)
    assert snapshot.summary.loadPercent == pytest.approx(50, abs=0.04)
    later = START + timedelta(seconds=50)
    snapshot = await legacy.build_snapshot(db, source, later)
    assert snapshot.summary.loadPercent is None and snapshot.dataState == "STALE"
    monkeypatch.setenv(
        "EDGE_V2_SIMULATION_HEIGHT_REFERENCES",
        json.dumps([{**REFERENCE, "environment_id": "changed"}]),
    )
    get_settings.cache_clear()
    for second in range(17, 22):
        _, snapshot = await legacy.frame(client, db, angle_context, second, 60)
        assert (snapshot.summary.loadPercent is not None) == (second == 21)
    assert snapshot.summary.loadPercent == pytest.approx(60, abs=0.04)


async def test_received_point_fill_retains_collection_confirmation_and_simulation_label(
    client, test_db_session, angle_context
):
    await legacy.test_simulation_collection_log_is_explicitly_labelled(
        client, test_db_session, angle_context
    )


async def test_simulation_height_history_produces_a_threshold_forecast(
    client, test_db_session, angle_context
):
    from src.app.services.fill_forecast_service import fill_forecast

    db = test_db_session
    await legacy.prepare(client, db)
    # Three complete/partial minute representatives after initial filter warm-up.
    # All scans are invented; no site dimensions or field prediction accuracy is assumed.
    for second in range(185):
        await legacy.frame(client, db, angle_context, second, 50 + second * 0.15)
    data = await fill_forecast(db, monitoring_source("pit-demo"), angle_context["now"])
    assert data.simulated and data.inputSource == "HEIGHT_SECTIONS"
    assert data.state == "AVAILABLE", data.model_dump()
    assert data.sampleCount == 3
    assert 70 < data.remainingSeconds < 100
    assert data.expectedThresholdAt > angle_context["now"]
    assert data.trendRSquared > 0.99
