"""Toy geometry only; no site coordinates, deployment settings or original drawings."""

import copy
import json

import pytest
from sqlalchemy import event, func, select
from test_daily_recordings import login as viewer_login
from test_lidar_height import REFERENCE as PHYSICAL
from test_lidar_height import inputs
from test_monitoring_v2 import DEVICE, SOURCE
from test_simulation_height import REFERENCE as SIMULATION
from test_simulation_height import calculate, raw_inputs

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.schemas.edge_v2 import Configuration
from src.app.services.measurement_layout_service import measurement_layout
from src.app.services.monitoring_v2_service import monitoring_source

URL = "/api/v2/monitoring/layout"


@pytest.fixture(autouse=True)
def layout_context(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def reference(value=SIMULATION):
    settings = get_settings()
    if "sensor_poses" in value:
        from src.app.schemas.lidar_height import SimulationHeightReference

        settings.EDGE_V2_SIMULATION_HEIGHT_REFERENCES = [
            SimulationHeightReference.model_validate(value)
        ]
        settings.EDGE_V2_HEIGHT_REFERENCES = []
    else:
        from src.app.schemas.lidar_height import HeightReference

        settings.EDGE_V2_HEIGHT_REFERENCES = [HeightReference.model_validate(value)]
        settings.EDGE_V2_SIMULATION_HEIGHT_REFERENCES = []


async def register(db, configuration=None):
    config = Configuration.model_validate(configuration or raw_inputs()[0])
    db.add(
        EdgeV2Message(
            site_id=config.site_id,
            edge_id=config.edge_id,
            message_id=config.message_id,
            message_type="edge_configuration",
            config_revision=config.config_revision,
            generated_at=config.generated_at,
            received_at=config.generated_at,
            body_hash="synthetic",
            payload=config.model_dump(mode="json"),
            compact=False,
        )
    )
    await db.commit()
    return config


async def layout(db):
    return await measurement_layout(db, monitoring_source("pit-demo"))


async def test_authentication_viewer_and_source_isolation(client, test_db_session):
    assert (await client.get(URL, params={"pit_id": "pit-demo"})).status_code == 401
    await viewer_login(client)
    r = await client.get(URL, params={"pit_id": "pit-demo"})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["state"] == "UNAVAILABLE"
    assert (await client.get(URL, params={"pit_id": "other"})).status_code == 404
    assert (await client.get(URL, params={"pit_id": ""})).status_code == 422
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Latest)) == 0


async def test_missing_reference_preserves_null_coordinates(test_db_session):
    result = await layout(test_db_session)
    assert result.source == "NONE" and result.region is None and result.coordinateFrame is None
    assert [s.sensorId for s in result.sensors] == ["lidar-a", "lidar-b"]
    assert all(s.position is None and s.measurementLine is None for s in result.sensors)


async def test_missing_registration_does_not_publish_reference_as_current_geometry(test_db_session):
    reference()
    result = await layout(test_db_session)
    assert result.reasonCodes == ["LAYOUT_CONFIGURATION_UNAVAILABLE"]
    assert result.region is None and result.simulated


async def test_simulation_geometry_frame_and_exact_a_to_b(test_db_session):
    reference()
    config = await register(test_db_session)
    result = await layout(test_db_session)
    assert result.state == "SIMULATION" and result.simulated
    assert result.source == "SIMULATION_REFERENCE" and result.bindingState == "UNKNOWN"
    assert result.coordinateFrame.id == config.coordinate_frame.frame_id
    assert result.unit == result.coordinateFrame.unit == "cm"
    assert result.region.kind == "SIMULATION_BOUNDARY"
    assert [(p.x, p.y) for p in result.region.boundary] == [(0, 0), (200, 0), (200, 200), (0, 200)]
    for sensor in result.sensors:
        assert sensor.position.model_dump() == {"x": 20, "y": 20, "z": 100}
        line = sensor.measurementLine
        assert line.a.model_dump() == {"x": 95, "y": 0, "z": 0}
        assert line.b.model_dump() == {"x": 95, "y": 200, "z": 0}
        assert line.lengthCm == 200
        assert sensor.calibrationState == "SIMULATION" and sensor.calibrationVersion is None
    encoded = result.model_dump_json()
    assert "environment_id" not in encoded and "simulator_revision" not in encoded
    assert "PHYSICAL_INSTALLATION_UNVERIFIED" in result.reasonCodes


async def test_layout_ratio_agrees_with_received_angle_profile(test_db_session):
    toy = copy.deepcopy(SIMULATION)
    toy["sampling_policy"] = "SENSOR_ANGLE"
    reference(toy)
    await register(test_db_session)
    result = await layout(test_db_session)
    profile = calculate(reference=toy)
    line = result.sensors[0].measurementLine
    assert line.a.y == 0 and line.b.y == 200
    assert [s["positionRatio"] for s in profile.samples] == pytest.approx(
        [45 / 200, 50 / 200, 55 / 200, 145 / 200, 150 / 200, 155 / 200], abs=0.001
    )


@pytest.mark.parametrize("change", ["frame", "sensors"])
async def test_configuration_mismatch_never_reuses_coordinates(test_db_session, change):
    reference()
    cfg, _ = raw_inputs()
    if change == "frame":
        cfg["coordinate_frame"]["frame_id"] = "different-frame"
    else:
        cfg["sensors"][0]["sensor_id"] = "different-sensor"
    await register(test_db_session, cfg)
    result = await layout(test_db_session)
    assert result.reasonCodes == ["LAYOUT_REFERENCE_MISMATCH"]
    assert result.coordinateFrame is None and result.region is None
    assert all(s.position is None for s in result.sensors)


async def test_live_config_mismatch_hides_old_layout_without_loading_scans(
    test_db_session, test_engine
):
    reference()
    await register(test_db_session)
    test_db_session.add(
        EdgeV2Latest(
            site_id="site-demo",
            edge_id="edge-demo",
            kind="measurement",
            message_id="latest",
            generated_at=Configuration.model_validate(raw_inputs()[0]).generated_at,
            payload={
                "config_revision": "new-config",
                "sensors": [{"private_scan_sentinel": [1] * 1000}],
            },
        )
    )
    await test_db_session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await layout(test_db_session)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert result.bindingState == "MISMATCH" and result.region is None
    assert result.reasonCodes == ["LAYOUT_LIVE_CONFIG_MISMATCH"]
    assert len(statements) == 1
    assert "private_scan_sentinel" not in result.model_dump_json()
    assert "edge_v2_latest.message_id" not in statements[0]


async def test_binding_matched_and_sensor_identity_not_array_order(test_db_session):
    reference()
    cfg, _ = raw_inputs()
    cfg["sensors"].reverse()
    config = await register(test_db_session, cfg)
    test_db_session.add(
        EdgeV2Latest(
            site_id="site-demo",
            edge_id="edge-demo",
            kind="status",
            message_id="latest",
            generated_at=config.generated_at,
            payload={"config_revision": "cfg-1"},
        )
    )
    await test_db_session.commit()
    result = await layout(test_db_session)
    assert result.bindingState == "MATCHED"
    assert [s.sensorId for s in result.sensors] == ["lidar-a", "lidar-b"]


async def test_no_floor_intersection_is_partial_and_not_a_zero_line(test_db_session):
    toy = copy.deepcopy(SIMULATION)
    for pose in toy["sensor_poses"].values():
        pose["direction0"], pose["direction90"] = [1, 0, 0], [0, 1, 0]
    reference(toy)
    await register(test_db_session)
    result = await layout(test_db_session)
    assert result.state == "PARTIAL" and result.simulated
    assert all(s.position is not None and s.measurementLine is None for s in result.sensors)
    assert all(s.reasonCodes == ["LAYOUT_FLOOR_SECTION_UNAVAILABLE"] for s in result.sensors)


async def test_verified_translation_is_converted_once_and_not_invented_scan_plane(test_db_session):
    reference(PHYSICAL)
    cfg, _ = inputs()
    cfg["sensors"][0]["calibration"]["translation_mm"] = [0, 200, 300]
    cfg["sensors"][1]["calibration"]["translation_mm"] = [400, 0, 600]
    await register(test_db_session, cfg)
    test_db_session.add(PitConfig(id="pit-demo", name="Synthetic", total_depth_cm=200))
    await test_db_session.commit()
    result = await layout(test_db_session)
    assert result.state == "PARTIAL" and not result.simulated
    assert result.region.kind == "HEIGHT_MEASUREMENT_REGION" and result.region.topZ == 150
    assert result.sensors[0].position.model_dump() == {"x": 0, "y": 20, "z": 30}
    assert result.sensors[1].position.model_dump() == {"x": 40, "y": 0, "z": 60}
    assert all(
        s.calibrationState == "VERIFIED" and s.measurementLine is None for s in result.sensors
    )
    assert all(
        s.profileMapping.axis == "x" and s.profileMapping.maximumCm == 100 for s in result.sensors
    )


@pytest.mark.parametrize("change", ["missing", "unverified", "version"])
async def test_unverified_or_wrong_calibration_has_no_fake_position(test_db_session, change):
    reference(PHYSICAL)
    cfg, _ = inputs()
    calibration = cfg["sensors"][0]["calibration"]
    if change == "missing":
        cfg["sensors"][0]["calibration"] = None
    elif change == "unverified":
        calibration["verification_state"] = "UNVERIFIED"
    else:
        calibration["version"] = "new-calibration"
    await register(test_db_session, cfg)
    result = await layout(test_db_session)
    assert result.sensors[0].position is None and result.sensors[0].measurementLine is None
    assert result.sensors[0].calibrationState == "UNAVAILABLE"
    assert result.sensors[1].calibrationState == "VERIFIED"
    assert result.region.topZ is None and "HEIGHT_DEPTH_UNAVAILABLE" in result.reasonCodes


async def test_unverified_physical_reference_has_no_claimed_geometry(test_db_session):
    toy = copy.deepcopy(PHYSICAL)
    toy["verified"] = False
    reference(toy)
    await register(test_db_session, inputs()[0])
    result = await layout(test_db_session)
    assert result.state == "UNVERIFIED" and result.region is None
    assert all(s.position is None for s in result.sensors)


async def test_repeated_reads_do_not_create_rows_or_notifications(test_db_session):
    reference()
    await register(test_db_session)
    for _ in range(3):
        await layout(test_db_session)
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Message)) == 1
    for model in [EdgeV2Latest, MediaClipJob, AlertLog]:
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0
