"""Invented geometry: edge coordinates, explicit calibration pins, and unchanged scan medians."""

import copy
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Configuration, Measurement
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.edge_v2_service import digest
from src.app.services.height_fill_service import height_method
from src.app.services.height_median_service import window_scope
from src.app.services.height_reference import reference_payload
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import lidar_profiles, monitoring_source
from src.app.services.simulation_height import simulation_profile
from tests import test_simulation_height as legacy
from tests.test_height_fill_pipeline import START, send
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_monitoring_v2 import login, status
from tests.test_sensor_angle_height import REFERENCE as ANGLE_REFERENCE
from tests.test_sensor_angle_height import angle_context as angle_context
from tests.test_sensor_angle_height import angle_inputs

REFERENCE = {
    **ANGLE_REFERENCE,
    "coordinate_source": "EDGE_WORLD",
    "calibration_versions": {"lidar-a": "edge-cal-a", "lidar-b": "edge-cal-b"},
}


def world_inputs(height=50.0, count=6):
    config, measurement = angle_inputs(height, count)
    for configured, sensor, name in zip(
        config["sensors"], measurement["sensors"], ["a", "b"], strict=True
    ):
        configured["calibration"] = {
            "version": f"edge-cal-{name}",
            "verification_state": "VERIFIED",
            "angle_sign": 1,
            "angle_zero_offset_mdeg": 0,
            "rotation": [[0.6, 0, 0.8], [0, 1, 0], [-0.8, 0, 0.6]],
            "translation_mm": [200, 200, 1000],
        }
        sensor.update(
            calibration_version=f"edge-cal-{name}",
            transform_state="VERIFIED",
            transform_reason_codes=[],
        )
        for i, point in enumerate(sensor["scan"]["points"]):
            # Deliberately independent of range: the consumer must use the provided coordinates.
            point.update(
                distance_mm=1,
                position_mm={
                    "x": round((20 + (100 - height) * 0.75) * 10),
                    "y": 500 + i,
                    "z": round(height * 10),
                },
                calculation_reason_codes=[],
            )
    return config, measurement


def calculate(config=None, measurement=None, reference=None, now=START):
    if config is None or measurement is None:
        c, m = world_inputs()
        config, measurement = config or c, measurement or m
    parsed = Measurement.model_validate(measurement)
    return simulation_profile(
        parsed,
        parsed.sensors[0],
        Configuration.model_validate(config),
        SimulationHeightReference.model_validate(reference or REFERENCE),
        200,
        now,
        30,
        1000,
    )


def test_world_height_uses_wire_coordinates_once_and_preserves_inputs(monkeypatch):
    c, m = world_inputs()
    original = copy.deepcopy((c, m))

    def unexpected_trigonometry(*args):
        raise AssertionError("World coordinates must not be recalculated from angles")

    monkeypatch.setattr("src.app.services.simulation_height.math.cos", unexpected_trigonometry)
    monkeypatch.setattr("src.app.services.simulation_height.math.sin", unexpected_trigonometry)
    profile = calculate(c, m)
    assert (c, m) == original
    assert profile.average == profile.sectionFillPercent == 50
    assert profile.simulated and profile.calibrationState == "SIMULATION"
    assert profile.valuePolicy == "SENSOR_ANGLE"
    assert "EDGE_WORLD_COORDINATES" in profile.reasonCodes
    assert all(s["height"] == 50 for s in profile.samples)


def test_nonzero_floor_is_subtracted_and_zero_z_is_a_valid_floor():
    c, m = world_inputs(height=40)
    p = calculate(c, m, {**REFERENCE, "floor_z_cm": 20, "top_z_cm": 120})
    assert p.average == p.sectionFillPercent == 20
    c, m = world_inputs(height=0)
    assert calculate(c, m).average == 0
    assert all(s["valid"] for s in calculate(c, m).samples)


@pytest.mark.parametrize("distance", [0, -1, 10**400])
def test_raw_range_does_not_override_a_valid_provided_coordinate(distance):
    c, m = world_inputs()
    for sensor in m["sensors"]:
        for point in sensor["scan"]["points"]:
            point["distance_mm"] = distance
    assert calculate(c, m).average == 50


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "sdk-invalid",
        "low-quality",
        "huge-x",
        "huge-y",
        "huge-z",
        "wall",
        "outside",
        "below",
        "above",
        "upward",
    ],
)
def test_invalid_coordinates_remain_gaps_without_a_range_fallback(change):
    c, m = world_inputs()
    point = m["sensors"][0]["scan"]["points"][0]
    point["distance_mm"] = 625  # The old range path would produce a valid 50cm height.
    reference = copy.deepcopy(REFERENCE)
    if change == "missing":
        point["position_mm"] = None
    elif change == "sdk-invalid":
        point["calculation_reason_codes"] = ["SDK_INVALID_RANGE"]
    elif change == "low-quality":
        reference["minimum_quality"] = 100
        point["quality"] = 99
    else:
        axis, value = {
            "huge-x": ("x", 10**400),
            "huge-y": ("y", -(10**400)),
            "huge-z": ("z", 10**400),
            "wall": ("x", 1),
            "outside": ("x", 2100),
            "below": ("z", -1),
            "above": ("z", 1001),
            "upward": ("z", 1000),
        }[change]
        point["position_mm"][axis] = value
    profile = calculate(c, m, reference)
    assert len(profile.samples) == 6
    assert profile.samples[0] == {
        "angleMdeg": 0,
        "sourceIndex": 0,
        "positionRatio": None,
        "height": None,
        "valid": False,
    }


@pytest.mark.parametrize(
    "change,reason",
    [
        ("transform", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("transform-reason", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("calibration-missing", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("unverified", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("version", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("pin", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("translation", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("rotation", "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("revision", "HEIGHT_REFERENCE_MISMATCH"),
        ("frame", "HEIGHT_REFERENCE_MISMATCH"),
        ("angles", "HEIGHT_REFERENCE_MISMATCH"),
        ("stale", "HEIGHT_SCAN_STALE"),
        ("clock", "HEIGHT_SCAN_CLOCK_UNAVAILABLE"),
        ("future", "HEIGHT_CLOCK_SKEW"),
    ],
)
def test_world_path_does_not_bypass_configuration_or_freshness(change, reason):
    c, m = world_inputs()
    ref = copy.deepcopy(REFERENCE)
    sensor = m["sensors"][0]
    calibration = c["sensors"][0]["calibration"]
    if change == "transform":
        sensor["transform_state"] = "UNAVAILABLE"
        for point in sensor["scan"]["points"]:
            point["position_mm"] = None
    elif change == "transform-reason":
        sensor["transform_reason_codes"] = ["UNVERIFIED"]
    elif change == "calibration-missing":
        c["sensors"][0]["calibration"] = None
    elif change == "unverified":
        calibration["verification_state"] = "UNVERIFIED"
    elif change == "version":
        sensor["calibration_version"] = "other"
    elif change == "pin":
        ref["calibration_versions"]["lidar-a"] = "other"
    elif change == "translation":
        calibration["translation_mm"][0] += 10
    elif change == "rotation":
        calibration["rotation"] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    elif change == "revision":
        m["config_revision"] = "new-config"
    elif change == "frame":
        m["coordinate_frame_id"] = "other"
    elif change == "angles":
        sensor["angle_intervals_mdeg"][0]["end_exclusive"] = 300000
    elif change == "stale":
        old = (START - timedelta(seconds=30)).isoformat()
        m["generated_at"] = old
        for observation in m["sensors"]:
            observation["scan"]["acquired_at"] = old
    elif change == "clock":
        sensor["scan"]["clock_state"] = "UNKNOWN"
    elif change == "future":
        m["generated_at"] = (START + timedelta(seconds=1)).isoformat()
    profile = calculate(c, m, ref)
    assert profile.calibrationState == "UNAVAILABLE"
    assert reason in profile.reasonCodes
    assert not profile.samples


@pytest.mark.parametrize("change", ["no-pins", "one-pin", "foreign-pin", "spatial", "unused-pins"])
def test_world_mode_requires_explicit_compatible_reference(change):
    ref = copy.deepcopy(REFERENCE)
    if change == "no-pins":
        ref.pop("calibration_versions")
    elif change == "one-pin":
        ref["calibration_versions"].pop("lidar-b")
    elif change == "foreign-pin":
        ref["calibration_versions"] = {"lidar-a": "a", "other": "b"}
    elif change == "spatial":
        ref["sampling_policy"] = "SPATIAL_BIN"
    else:
        ref["coordinate_source"] = "RAW_RANGE"
    with pytest.raises(ValidationError):
        SimulationHeightReference.model_validate(ref)


@pytest.fixture
def world_context(angle_context, monkeypatch):
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    monkeypatch.setattr(legacy, "raw_inputs", world_inputs)
    get_settings.cache_clear()
    yield angle_context
    get_settings.cache_clear()


async def test_edge_points_keep_current_graph_and_five_second_scan_mean_median(
    client, test_db_session, world_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    await login(client)
    for second in range(8):
        _, snapshot = await legacy.frame(
            client, db, world_context, second, 90 if second == 6 else 50
        )
        page = await lidar_profiles(db, monitoring_source("pit-demo"), world_context["now"])
        if second >= 4:
            assert snapshot.summary.loadPercent == 50
            assert page.profiles[0].filteredAverage == 50
        if second == 6:
            assert all(s["height"] == 90 for s in page.profiles[0].samples)
            assert page.profiles[0].average == 90
            response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
            assert response.status_code == 200
            profile = response.json()["profiles"][0]
            assert profile["unit"] == "cm" and profile["average"] == 90
            assert profile["filteredAverage"] == 50
            assert "EDGE_WORLD_COORDINATES" in snapshot.fillObservation.reason_codes
    assert all(p.summaryValuePolicy == "SCAN_MEAN_TEMPORAL_MEDIAN" for p in page.profiles)
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=8), 60
    )
    assert history.samples[0].simulated
    assert history.samples[0].averagePercent == 50
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert all(
        len(s["frames"]) == 5 and all(len(f["heights"]) == 1 for f in s["frames"])
        for s in window.payload["sensors"].values()
    )

    def missing_coordinates(height=50):
        config, measurement = world_inputs(height)
        for sensor in measurement["sensors"]:
            for point in sensor["scan"]["points"]:
                point["position_mm"] = None
        return config, measurement

    monkeypatch.setattr(legacy, "raw_inputs", missing_coordinates)
    _, missing = await legacy.frame(client, db, world_context, 8)
    page = await lidar_profiles(db, monitoring_source("pit-demo"), world_context["now"])
    assert missing.summary.loadPercent is None
    assert all(p.average is None and p.filteredAverage is None for p in page.profiles)
    assert all(s["height"] is None for p in page.profiles for s in p.samples)


async def test_default_raw_reference_keeps_method_identity_and_world_switch_resets_scope(
    client, test_db_session, angle_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    await legacy.frame(client, db, angle_context, 0)
    source = monitoring_source("pit-demo")
    reference = SimulationHeightReference.model_validate(ANGLE_REFERENCE)
    old = reference.model_dump(mode="json", exclude={"coordinate_source", "calibration_versions"})
    assert reference_payload(reference) == old
    old_method = "SIMULATION_SENSOR_ANGLE_V1:" + digest([old, 100.0, 5])
    assert height_method(source, 200) == old_method
    scope = await window_scope(db, source, angle_context["now"])
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    get_settings.cache_clear()
    assert height_method(source, 200) != old_method
    assert await window_scope(db, source, angle_context["now"]) != scope
    page = await lidar_profiles(db, source, angle_context["now"])
    assert all(p.filteredAverage is None for p in page.profiles)


async def test_new_revision_requires_an_explicit_reference_update_and_preserves_receipts(
    client, test_db_session, world_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    for second in range(5):
        body, _ = await legacy.frame(client, db, world_context, second)
    c, m = world_inputs()
    c.update(message_id="new-config", config_revision="cfg-2")
    assert (await send(client, c)).status_code == 201
    m.update(message_id="new-revision", config_revision="cfg-2")
    now = START + timedelta(seconds=5)
    world_context["now"] = now
    report = status("new-status-revision", now, 6)
    report["config_revision"] = "cfg-2"
    report["clock"]["state"] = "UNSYNCED"
    assert (await send(client, report)).status_code == 201
    m["generated_at"] = now.isoformat()
    for i, sensor in enumerate(m["sensors"]):
        sensor["scan"].update(
            scan_id=f"new-revision-scan-{i}", sequence=5, acquired_at=now.isoformat()
        )
    assert (await send(client, m)).status_code == 201
    page = await lidar_profiles(db, monitoring_source("pit-demo"), now)
    assert all("HEIGHT_REFERENCE_MISMATCH" in p.reasonCodes for p in page.profiles)
    assert (await send(client, body)).json()["duplicate"]
    # Changing only the allowed revision must not retain any previous filter window.
    monkeypatch.setenv(
        "EDGE_V2_SIMULATION_HEIGHT_REFERENCES",
        json.dumps([{**REFERENCE, "config_revision": "cfg-2"}]),
    )
    get_settings.cache_clear()
    assert height_method(monitoring_source("pit-demo"), 200) is not None
    page = await lidar_profiles(db, monitoring_source("pit-demo"), now)
    assert all(p.average == 50 and p.filteredAverage is None for p in page.profiles)
