"""Synthetic geometry and HTTP scenarios; no physical sensor or field calibration values."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from src.app.core.config import Settings, get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.schemas.edge_v2 import Configuration, Measurement
from src.app.schemas.lidar_height import HeightReference
from src.app.services.lidar_height import height_profile
from src.app.services.monitoring_v2_service import lidar_profiles, monitoring_source
from tests.test_edge_v2 import configuration, measurement, sensor
from tests.test_monitoring_v2 import DEVICE, SOURCE, TOKEN, assessment, login, status

NOW = datetime(2030, 1, 1, tzinfo=UTC)
REFERENCE = {
    "pit_id": "pit-demo",
    "config_revision": "cfg-1",
    "coordinate_frame_id": "frame-demo",
    "calibration_versions": {"lidar-a": "cal-a-1", "lidar-b": "cal-b-1"},
    "verified": True,
    "floor_z_cm": -50.0,
    "x_min_cm": 0.0,
    "x_max_cm": 100.0,
    "y_min_cm": 0.0,
    "y_max_cm": 100.0,
    "profile_axis": "x",
    "bin_count": 2,
    "minimum_points_per_bin": 3,
}


def inputs():
    c = configuration()
    c["generated_at"] = NOW.isoformat()
    for s in c["sensors"]:
        s["calibration"]["verification_state"] = "VERIFIED"
    m = measurement(count=6)
    m.update(generated_at=NOW.isoformat(), pair_state="MATCHED", pair_delta_ms=0.0)
    m["sensors"][1] = sensor("b", count=6)
    for number, s in enumerate(m["sensors"]):
        s.update(transform_state="VERIFIED", transform_reason_codes=[])
        s["scan"]["acquired_at"] = NOW.isoformat()
        for i, p in enumerate(s["scan"]["points"]):
            # Two equal-width regions, different heights; B has higher representative heights.
            p["position_mm"] = {
                "x": 250 if i < 3 else 750,
                "y": 500,
                "z": (0 if i < 3 else 500) + number * 500,
            }
    return c, m


def calculate(c=None, m=None, r=None, now=NOW, depth=200.0):
    original_c, original_m = inputs()
    config = Configuration.model_validate(c or original_c)
    observation = Measurement.model_validate(m or original_m)
    return height_profile(
        observation,
        observation.sensors[0],
        config,
        HeightReference.model_validate(r or REFERENCE),
        depth,
        now,
        30,
        1000.0,
    )


def test_geometry_units_floor_and_equal_width_bin_weighting():
    c, m = inputs()
    original = copy.deepcopy(m)
    result = calculate(c, m)
    assert result.calibrationState == "VERIFIED"
    assert (result.minimum, result.maximum, result.average) == (50.0, 100.0, 75.0)
    assert result.sectionFillPercent == 37.5
    assert result.coverageRatio == 1.0
    assert result.samples == [
        {"positionRatio": 0.25, "height": 50.0, "valid": True},
        {"positionRatio": 0.75, "height": 100.0, "valid": True},
    ]
    assert m == original
    # More points in the first region must not overweight that region.
    scan = m["sensors"][0]["scan"]
    for i in range(6, 106):
        p = copy.deepcopy(scan["points"][0])
        p.update(index=i, angle_mdeg=i * 100)
        scan["points"].append(p)
    scan["source_point_count"] = scan["selected_point_count"] = 106
    assert calculate(c, m).average == result.average


def test_spike_rejection_invalid_gaps_and_region_bounds():
    c, m = inputs()
    points = m["sensors"][0]["scan"]["points"]
    points[0]["position_mm"]["z"] = 1400
    assert calculate(c, m).minimum == 50.0
    points[3]["calculation_reason_codes"] = ["SDK_INVALID_RANGE"]
    result = calculate(c, m)
    assert result.samples[1] == {"positionRatio": 0.75, "height": None, "valid": False}
    assert result.sectionFillPercent is None
    assert result.coverageRatio == 0.5
    assert result.reasonCodes == ["HEIGHT_INSUFFICIENT_COVERAGE"]
    for p in points:
        p["position_mm"]["y"] = 1001  # Outside the configured region.
    assert calculate(c, m).reasonCodes == ["HEIGHT_NO_VALID_BINS"]


@pytest.mark.parametrize(
    "change", ["null", "negative_range", "below_floor", "above_depth", "quality", "reason", "huge"]
)
def test_invalid_points_never_become_zero_height(change):
    c, m = inputs()
    r = copy.deepcopy(REFERENCE)
    for p in m["sensors"][0]["scan"]["points"]:
        if change == "null":
            p["position_mm"] = None
        elif change == "negative_range":
            p["distance_mm"] = -1
        elif change == "below_floor":
            p["position_mm"]["z"] = -501
        elif change == "above_depth":
            p["position_mm"]["z"] = 1501
        elif change == "reason":
            p["calculation_reason_codes"] = ["SDK_INVALID_RANGE"]
        elif change == "quality":
            r["minimum_quality"] = 50
            p["quality"] = 49
        elif change == "huge":
            p["position_mm"]["z"] = 10**400
    result = calculate(c, m, r)
    assert result.average is result.sectionFillPercent is None
    assert all(s["height"] is None and not s["valid"] for s in result.samples)


@pytest.mark.parametrize(
    "change",
    [
        "unverified_reference",
        "config_revision",
        "frame",
        "calibration_version",
        "unverified_calibration",
        "unavailable_transform",
        "angles",
        "stale",
        "future",
        "clock",
        "depth",
        "other_device",
    ],
)
def test_unusable_context_returns_null(change):
    c, m = inputs()
    r = copy.deepcopy(REFERENCE)
    now, depth = NOW, 200.0
    if change == "unverified_reference":
        r["verified"] = False
    elif change == "config_revision":
        r["config_revision"] = "cfg-other"
    elif change == "frame":
        r["coordinate_frame_id"] = "frame-other"
    elif change == "calibration_version":
        r["calibration_versions"]["lidar-a"] = "cal-other"
    elif change == "unverified_calibration":
        c["sensors"][0]["calibration"]["verification_state"] = "UNVERIFIED"
    elif change == "unavailable_transform":
        c["sensors"][0]["calibration"] = None
        m["sensors"][0].update(
            transform_state="UNAVAILABLE", transform_reason_codes=["CALIBRATION_MISSING"]
        )
        for p in m["sensors"][0]["scan"]["points"]:
            p["position_mm"] = None
    elif change == "angles":
        c["sensors"][0]["angle_intervals_mdeg"][0]["end_exclusive"] = 90000
    elif change == "stale":
        now += timedelta(seconds=30)
    elif change == "future":
        now -= timedelta(seconds=1)
    elif change == "clock":
        m["sensors"][0]["scan"]["clock_state"] = "UNKNOWN"
    elif change == "depth":
        depth = None
    elif change == "other_device":
        c["edge_id"] = "edge-other"
    result = calculate(c, m, r, now, depth)
    assert result.average is result.sectionFillPercent is None
    assert result.samples == [] and result.reasonCodes


def test_y_axis_boundary_and_empty_floor():
    c, m = inputs()
    r = {**REFERENCE, "profile_axis": "y"}
    for i, p in enumerate(m["sensors"][0]["scan"]["points"]):
        p["position_mm"] = {"x": 500, "y": 0 if i < 3 else 1000, "z": -500}
    result = calculate(c, m, r)
    assert result.sectionFillPercent == 0.0
    assert all(s["height"] == 0 and s["valid"] for s in result.samples)


@pytest.mark.parametrize(
    "change",
    ["reversed_region", "nan", "wrong_sensor", "duplicate_pit", "missing_pit", "compact_disabled"],
)
def test_reference_settings_fail_before_startup(change):
    r = copy.deepcopy(REFERENCE)
    refs, sources, compact = [r], [SOURCE], True
    if change == "reversed_region":
        r["x_max_cm"] = r["x_min_cm"]
    elif change == "nan":
        r["floor_z_cm"] = float("nan")
    elif change == "wrong_sensor":
        r["calibration_versions"] = {"a": "ca", "b": "cb"}
    elif change == "duplicate_pit":
        refs.append(r)
    elif change == "missing_pit":
        sources = []
    elif change == "compact_disabled":
        compact = False
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            EDGE_V2_DEVICES=[DEVICE],
            EDGE_V2_MONITORING_SOURCES=sources,
            EDGE_V2_COMPACT_STORAGE_ENABLED=compact,
            EDGE_V2_HEIGHT_REFERENCES=refs,
        )


@pytest.fixture
def height_settings(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    get_settings.cache_clear()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.edge_v2_service.datetime", Clock)
    monkeypatch.setattr("src.app.services.monitoring_v2_service.datetime", Clock)
    yield
    get_settings.cache_clear()


async def send(client, payload):
    route = {
        "edge_configuration": "configurations",
        "lidar_measurement": "measurements",
        "edge_status": "status",
        "edge_assessment": "assessments",
    }[payload["message_type"]]
    return await client.post(
        "/api/edge/v2/" + route,
        json=payload,
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": payload["message_id"]},
    )


async def prepare(client, db):
    c, m = inputs()
    s = status(when=NOW)
    s["config_revision"] = "cfg-1"
    db.add(PitConfig(id="pit-demo", name="Synthetic", total_depth_cm=200))
    await db.commit()
    for p in [c, s, m]:
        response = await send(client, p)
        assert response.status_code == 201, response.text
    return c, s, m


async def test_profiles_http_and_receipt_do_not_create_edge_decisions(
    client,
    test_db_session,
    height_settings,
):
    db = test_db_session
    c, s, m = await prepare(client, db)
    route = "/api/v2/monitoring/profiles?pit_id=pit-demo"
    assert (await client.get(route)).status_code == 401
    await login(client)
    response = await client.get(route)
    assert response.status_code == 200, response.text
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert [p["average"] for p in body["profiles"]] == [75.0, 125.0]
    assert body["meanSectionFillPercent"] == 50.0
    assert (await send(client, m)).json()["duplicate"] is True
    changed = copy.deepcopy(m)
    changed["sensors"][0]["scan"]["points"][0]["quality"] += 1
    assert (await send(client, changed)).status_code == 409
    assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 3
    assert await db.scalar(select(func.count()).select_from(EdgeV2AlertLink)) == 0
    # This projection cannot turn an unimplemented edge fill capability into a valid decision.
    a = assessment(when=NOW)
    a.update(
        config_revision="cfg-1",
        rule_version=None,
        decision_state="UNAVAILABLE",
        fill_state="UNAVAILABLE",
        fill_estimate=None,
    )
    a["capabilities"]["fill"] = "NOT_IMPLEMENTED"
    assert (await send(client, a)).status_code == 201
    snapshot = await client.get("/api/v2/monitoring/snapshot?pit_id=pit-demo")
    assert snapshot.json()["summary"]["loadPercent"] is None
    assert snapshot.json()["assessment"]["capabilities"]["fill"] == "NOT_IMPLEMENTED"


async def test_profiles_status_timeout_and_sensor_failure(client, test_db_session, height_settings):
    db = test_db_session
    c, s, m = await prepare(client, db)
    source = monitoring_source("pit-demo")
    page = await lidar_profiles(db, source, NOW + timedelta(seconds=30))
    assert page.meanSectionFillPercent is None
    assert all(p.average is None for p in page.profiles)
    s.update(message_id="status-2", sequence=2)
    s["sensors"][1]["data_state"] = "STALE"
    assert (await send(client, s)).status_code == 201
    page = await lidar_profiles(db, source, NOW)
    assert page.profiles[0].average == 75.0
    assert page.profiles[1].average is None
    assert page.meanSectionFillPercent is None


@pytest.mark.parametrize("change", ["clock", "revision", "future", "no_scan_time"])
async def test_profiles_reject_unusable_status(client, test_db_session, height_settings, change):
    db = test_db_session
    c, s, m = await prepare(client, db)
    s.update(message_id="status-2", sequence=2)
    if change == "clock":
        s["clock"]["state"] = "UNKNOWN"
    elif change == "revision":
        s["config_revision"] = "cfg-other"
    elif change == "future":
        s["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "no_scan_time":
        for sensor in s["sensors"]:
            sensor["last_scan_at"] = None
    assert (await send(client, s)).status_code == 201
    page = await lidar_profiles(db, monitoring_source("pit-demo"), NOW)
    assert page.meanSectionFillPercent is None
    assert all(p.average is None and not p.samples for p in page.profiles)


async def test_profiles_config_arrives_late_and_unmatched_pair(
    client, test_db_session, height_settings
):
    c, m = inputs()
    s = status(when=NOW)
    s["config_revision"] = "cfg-1"
    db = test_db_session
    db.add(PitConfig(id="pit-demo", name="Synthetic", total_depth_cm=200))
    await db.commit()
    for p in [s, m]:
        assert (await send(client, p)).status_code == 201
    source = monitoring_source("pit-demo")
    page = await lidar_profiles(db, source, NOW)
    assert page.profiles[0].reasonCodes == ["HEIGHT_CONFIGURATION_UNAVAILABLE"]
    assert (await send(client, c)).status_code == 201
    assert (await lidar_profiles(db, source, NOW)).meanSectionFillPercent == 50.0
    m.update(
        message_id="m-2",
        generated_at=(NOW + timedelta(seconds=1)).isoformat(),
        pair_state="UNMATCHED",
        pair_delta_ms=101.0,
    )
    m["sensors"][1]["scan"]["acquired_monotonic_ns"] = str(1000950000000 + 101000000)
    # New scans use distinct immutable IDs when the pair changes.
    m["sensors"][1]["scan"].update(scan_id="scan-b-2", sequence=2)
    assert (await send(client, m)).status_code == 201
    page = await lidar_profiles(db, source, NOW + timedelta(seconds=1))
    assert page.meanSectionFillPercent is None
    assert page.meanSectionReasonCodes == ["HEIGHT_PAIR_UNMATCHED"]
    assert all(p.average is not None for p in page.profiles)
