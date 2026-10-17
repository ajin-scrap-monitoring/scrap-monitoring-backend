"""Public toy geometry only: raw ranges, unavailable transforms and unverified UTC."""

import copy
import json
import math
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from src.app.core.config import Settings, get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.schemas.edge_v2 import Configuration, Measurement
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.edge_v2_operations import process_source
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from src.app.services.simulation_height import simulation_profile
from tests.test_height_fill_pipeline import START, send
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_lidar_height import REFERENCE as VERIFIED_REFERENCE
from tests.test_lidar_height import inputs
from tests.test_monitoring_v2 import DEVICE, SOURCE, login, status

# Deliberately small invented box, unrelated to any site's dimensions or simulator deployment.
POSE = {"origin_cm": [20, 20, 100], "direction0": [0.6, 0, -0.8], "direction90": [0, 1, 0]}
REFERENCE = {
    "pit_id": "pit-demo",
    "config_revision": "cfg-1",
    "coordinate_frame_id": "frame-demo",
    "environment_id": "synthetic-test-box",
    "simulator_revision": "a" * 40,
    "floor_z_cm": 0,
    "top_z_cm": 100,
    "boundary_xy_cm": [[0, 0], [200, 0], [200, 200], [0, 200]],
    "sensor_poses": {"lidar-a": POSE, "lidar-b": POSE},
    "sampling_policy": "SPATIAL_BIN",
    "bin_count": 2,
    "minimum_points_per_bin": 3,
}


def raw_inputs(height=50.0):
    c, m = inputs()
    for sensor in c["sensors"]:
        sensor["calibration"] = None
    for sensor in m["sensors"]:
        sensor.update(
            calibration_version=None,
            transform_state="UNAVAILABLE",
            transform_reason_codes=["CALIBRATION_MISSING"],
        )
        sensor["scan"]["clock_state"] = "UNSYNCED"
        for point, y in zip(sensor["scan"]["points"], (45, 50, 55, 145, 150, 155), strict=True):
            angle = round(math.degrees(math.atan2(y - 20, (100 - height) / 0.8)) * 1000)
            distance = round((100 - height) / 0.8 / math.cos(math.radians(angle / 1000)) * 10)
            point.update(
                angle_mdeg=angle,
                distance_mm=distance,
                position_mm=None,
                calculation_reason_codes=["CALIBRATION_MISSING"],
            )
    return c, m


def calculate(c=None, m=None, reference=None, now=START):
    config, raw = raw_inputs()
    measurement = Measurement.model_validate(m or raw)
    return simulation_profile(
        measurement,
        measurement.sensors[0],
        Configuration.model_validate(c or config),
        SimulationHeightReference.model_validate(reference or REFERENCE),
        200,
        now,
        30,
        1000,
    )


def test_raw_range_geometry_preserves_wire_and_exposes_simulation_provenance():
    c, m = raw_inputs()
    original = copy.deepcopy(m)
    p = calculate(c, m)
    assert m == original
    assert p.calibrationState == "SIMULATION" and p.simulated
    assert p.timeBasis == "SIMULATION_SOURCE_UTC_UNVERIFIED"
    assert p.referenceDepthCm == 100 and p.sectionLengthCm == 200
    assert p.average == pytest.approx(50, abs=0.04)
    assert p.sectionFillPercent == pytest.approx(50, abs=0.04)
    assert p.coverageRatio == 1
    assert all(s["valid"] for s in p.samples)
    # More points in one bin do not change its weight; an isolated reflection is rejected.
    for sensor in m["sensors"]:
        sensor["scan"]["points"][0]["distance_mm"] -= 200
    assert calculate(c, m).average == pytest.approx(50, abs=0.04)


def test_floor_zero_is_valid_and_missing_bins_are_null():
    _, m = raw_inputs()
    for s in m["sensors"]:
        for p in s["scan"]["points"]:
            p.update(angle_mdeg=0, distance_mm=1250)
    reference = {**REFERENCE, "minimum_coverage": 0.5}
    result = calculate(m=m, reference=reference)
    assert result.average == result.sectionFillPercent == 0
    assert result.samples[0]["height"] == 0 and result.samples[0]["valid"]
    assert result.samples[1]["height"] is None and not result.samples[1]["valid"]
    assert calculate(m=m).sectionFillPercent is None


@pytest.mark.parametrize("distance", [0, -1, 10**400, 1])
def test_invalid_ranges_never_become_floor_or_height(distance):
    _, m = raw_inputs()
    for s in m["sensors"]:
        for p in s["scan"]["points"]:
            p["distance_mm"] = distance
    assert calculate(m=m).sectionFillPercent is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"coordinate_frame_id": "other"}, "HEIGHT_REFERENCE_MISMATCH"),
        ({"config_revision": "other"}, "HEIGHT_REFERENCE_MISMATCH"),
        ({"generated_at": (START + timedelta(seconds=1)).isoformat()}, "HEIGHT_CLOCK_SKEW"),
    ],
)
def test_reference_and_future_time_guards(change, reason):
    _, m = raw_inputs()
    m.update(change)
    assert reason in calculate(m=m).reasonCodes


def test_stale_clock_and_upward_rays_are_rejected():
    _, m = raw_inputs()
    assert "HEIGHT_SCAN_STALE" in calculate(m=m, now=START + timedelta(seconds=30)).reasonCodes
    m["sensors"][0]["scan"]["clock_state"] = "BROKEN"
    assert "HEIGHT_SCAN_CLOCK_UNAVAILABLE" in calculate(m=m).reasonCodes
    m["sensors"][0]["scan"]["clock_state"] = "SYNCED"
    for p in m["sensors"][0]["scan"]["points"]:
        p["angle_mdeg"] = 170000
    assert calculate(m=m).sectionFillPercent is None


@pytest.mark.parametrize(
    "change",
    [
        {"top_z_cm": 0},
        {"floor_z_cm": float("nan")},
        {"boundary_xy_cm": [[0, 0], [200, 200], [0, 200], [200, 0]]},
        {"boundary_xy_cm": [[0, 0], [100, 0], [100, 0], [0, 100]]},
        {"sensor_poses": {"lidar-a": {**POSE, "direction90": [0.6, 0, -0.8]}, "lidar-b": POSE}},
        {"sensor_poses": {"lidar-a": {**POSE, "direction0": [1, 1, 1]}, "lidar-b": POSE}},
    ],
)
def test_bad_geometry_fails_before_receiving_scans(change):
    with pytest.raises(ValidationError):
        SimulationHeightReference.model_validate({**REFERENCE, **change})


def test_simulation_requires_explicit_development_assignment_and_compact_storage():
    common = {
        "_env_file": None,
        "EDGE_V2_DEVICES": [DEVICE],
        "EDGE_V2_MONITORING_SOURCES": [SOURCE],
        "EDGE_V2_SIMULATION_HEIGHT_REFERENCES": [REFERENCE],
        "EDGE_V2_COMPACT_STORAGE_ENABLED": True,
    }
    assert Settings(**common).EDGE_V2_SIMULATION_HEIGHT_REFERENCES
    for change in (
        {"APP_ENV": "production"},
        {"EDGE_V2_COMPACT_STORAGE_ENABLED": False},
        {"EDGE_V2_HEIGHT_REFERENCES": [VERIFIED_REFERENCE]},
        {
            "EDGE_V2_SIMULATION_HEIGHT_REFERENCES": [
                {**REFERENCE, "sensor_poses": {"other": POSE, "lidar-b": POSE}}
            ]
        },
    ):
        with pytest.raises(ValidationError):
            Settings(**(common | change))


@pytest.fixture
def simulation_context(height_context, monkeypatch):
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", "[]")
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    monkeypatch.setenv("EDGE_V2_HEIGHT_MEDIAN_ENABLED", "true")
    get_settings.cache_clear()
    yield height_context
    get_settings.cache_clear()


async def prepare(client, db):
    db.add(
        PitConfig(
            id="pit-demo",
            name="Synthetic",
            total_depth_cm=200,
            warning_threshold_percent=75,
            critical_threshold_percent=90,
        )
    )
    await db.commit()
    c, _ = raw_inputs()
    assert (await send(client, c)).status_code == 201


async def frame(client, db, clock, second, height=50, sensor_failure=False):
    now = START + timedelta(seconds=second)
    clock["now"] = now
    s = status(f"status-{second}", now, second + 1)
    s["config_revision"] = "cfg-1"
    s["clock"]["state"] = "UNSYNCED"
    if sensor_failure:
        s["sensors"][1]["data_state"] = "STALE"
    assert (await send(client, s)).status_code == 201
    _, m = raw_inputs(height)
    m.update(message_id=f"measurement-{second}", generated_at=now.isoformat())
    for i, sensor in enumerate(m["sensors"]):
        sensor["scan"].update(
            scan_id=f"scan-{i}-{second}",
            sequence=second,
            acquired_at=now.isoformat(),
            acquired_monotonic_ns=str((second + 1) * 10**9),
        )
    r = await send(client, m)
    assert r.status_code == 201, r.text
    await process_source(db, monitoring_source("pit-demo"), now)
    return m, await build_snapshot(db, monitoring_source("pit-demo"), now)


async def test_existing_profiles_snapshot_and_history_share_filtered_height(
    client,
    test_db_session,
    simulation_context,
):
    db = test_db_session
    await prepare(client, db)
    await login(client)
    for second in range(12):
        payload, snapshot = await frame(
            client, db, simulation_context, second, 95 if second == 7 else 50
        )
        if second < 4:
            assert snapshot.summary.loadPercent is None
        else:
            assert snapshot.summary.loadPercent == pytest.approx(50, abs=0.04)
        assert snapshot.summary.simulated
        assert snapshot.fillObservation.simulated
        assert snapshot.fillObservation.time_basis == "SIMULATION_SOURCE_UTC_UNVERIFIED"
        assert snapshot.deviceStatus.snapshot.clock.state == "UNSYNCED"
    assert snapshot.assessment is None
    response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
    assert response.status_code == 200
    for p in response.json()["profiles"]:
        assert p["simulated"] and p["calibrationState"] == "SIMULATION"
        assert p["average"] == pytest.approx(50, abs=0.04)
        assert p["valuePolicy"] == "SPATIAL_TEMPORAL_MEDIAN"
        assert p["referenceDepthCm"] == 100  # Stored pit depth remains 200.
    response = await client.get("/api/v2/monitoring/snapshot?pit_id=pit-demo")
    assert response.status_code == 200 and response.json()["summary"]["simulated"]
    response = await client.get(
        "/api/v2/monitoring/load-history",
        params={
            "pit_id": "pit-demo",
            "from": START.isoformat(),
            "to": (START + timedelta(seconds=12)).isoformat(),
            "interval": 60,
        },
    )
    assert response.status_code == 200, response.text
    sample = response.json()["samples"][0]
    assert sample["simulated"] and sample["methodVersion"].startswith(
        "SIMULATION_HEIGHT_SECTION_V1:"
    )
    assert sample["averagePercent"] == pytest.approx(50, abs=0.04)
    assert sample["validSamples"] == 8 and sample["invalidSamples"] == 4
    assert (await send(client, payload)).json()["duplicate"]
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert all(len(s["frames"]) == 5 for s in window.payload["sensors"].values())
    assert "position_mm" not in json.dumps(window.payload)
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0
    assert (await db.get(PitConfig, "pit-demo")).total_depth_cm == 200
    latest = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "measurement"))
    assert latest.payload == payload


async def test_simulation_status_failure_stale_boot_and_reference_change_invalidate(
    client,
    test_db_session,
    simulation_context,
    monkeypatch,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, simulation_context, second)
    _, snapshot = await frame(client, db, simulation_context, 5, sensor_failure=True)
    assert snapshot.summary.loadPercent is None and snapshot.dataState == "UNAVAILABLE"
    assert (await db.get(EdgeV2Filter, ("site-demo", "edge-demo"))).payload["samples"] == []
    for second in range(6, 11):
        _, snapshot = await frame(client, db, simulation_context, second)
    assert snapshot.summary.loadPercent is not None
    later = START + timedelta(seconds=50)
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), later)
    assert snapshot.summary.loadPercent is None and snapshot.dataState == "STALE"
    s = status("boot-change", START + timedelta(seconds=11), 0)
    s.update(config_revision="cfg-1", orchestrator_instance_id="new-instance")
    s["clock"]["state"] = "UNSYNCED"
    simulation_context["now"] = START + timedelta(seconds=11)
    assert (await send(client, s)).status_code == 201
    assert (
        await build_snapshot(db, monitoring_source("pit-demo"), simulation_context["now"])
    ).summary.loadPercent is None
    monkeypatch.setenv(
        "EDGE_V2_SIMULATION_HEIGHT_REFERENCES",
        json.dumps([{**REFERENCE, "environment_id": "changed"}]),
    )
    get_settings.cache_clear()
    for second in range(12, 17):
        _, snapshot = await frame(client, db, simulation_context, second, 60)
    assert snapshot.summary.loadPercent == pytest.approx(60, abs=0.04)
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=17), 60
    )
    sample = history.samples[0]
    assert sample.simulated and sample.inputSource == "MIXED" and sample.valuePercent is None
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", "[]")
    monkeypatch.setenv("EDGE_V2_HEIGHT_FILL_ENABLED", "false")
    monkeypatch.setenv("EDGE_V2_HEIGHT_MEDIAN_ENABLED", "false")
    get_settings.cache_clear()
    page = await lidar_profiles(db, monitoring_source("pit-demo"), simulation_context["now"])
    assert all(p.average is None and not p.simulated for p in page.profiles)


async def test_simulation_collection_log_is_explicitly_labelled(
    client, test_db_session, simulation_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(8):
        _, snapshot = await frame(client, db, simulation_context, second, 95)
    assert snapshot.operations.effectiveState == "CRITICAL"
    log = await db.scalar(select(AlertLog))
    assert (
        log.message.startswith("[시뮬레이션]") and log.channel == "LOG" and log.status == "RECORDED"
    )
    await login(client)
    alerts = (await client.get("/api/v2/monitoring/alerts?pit_id=pit-demo")).json()
    assert alerts["items"][0]["simulated"]


async def test_repeated_scan_new_envelope_and_gap_do_not_finish_warming(
    client,
    test_db_session,
    simulation_context,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(3):
        payload, snapshot = await frame(client, db, simulation_context, second)
    assert snapshot.summary.loadPercent is None
    assert (await send(client, payload)).json()["duplicate"]
    changed = copy.deepcopy(payload)
    changed["sensors"][0]["scan"]["points"][0]["quality"] += 1
    assert (await send(client, changed)).status_code == 409
    # A later envelope holding the exact same scan must not provide a new second.
    repeated = copy.deepcopy(payload)
    repeated.update(
        message_id="repeated-scan", generated_at=(START + timedelta(seconds=3)).isoformat()
    )
    simulation_context["now"] = START + timedelta(seconds=3)
    assert (await send(client, repeated)).status_code == 201
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), simulation_context["now"])
    assert snapshot.summary.loadPercent is None
    for second in range(10, 15):
        _, snapshot = await frame(client, db, simulation_context, second)
        assert (snapshot.summary.loadPercent is not None) == (second == 14)


def test_unsynced_permission_is_scoped_to_simulation_only(monkeypatch):
    from src.app.schemas.lidar_height import HeightReference
    from src.app.services.height_reference import clock_allowed
    from src.app.services.lidar_height import height_profile

    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    get_settings.cache_clear()
    try:
        assert clock_allowed("pit-demo", "UNSYNCED")
        assert not clock_allowed("another-pit", "UNSYNCED")
        assert not clock_allowed("pit-demo", "BROKEN")
        c, m = inputs()
        m["sensors"][0]["scan"]["clock_state"] = "UNSYNCED"
        raw = Measurement.model_validate(m)
        normal = height_profile(
            raw,
            raw.sensors[0],
            Configuration.model_validate(c),
            HeightReference.model_validate(VERIFIED_REFERENCE),
            200,
            START,
            30,
            1000,
        )
        assert normal.average is None and "HEIGHT_SCAN_CLOCK_UNAVAILABLE" in normal.reasonCodes
    finally:
        get_settings.cache_clear()
