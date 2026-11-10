"""Independent synthetic cloud fixtures; no production geometry or captured inputs."""

import copy

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.schemas.edge_v2 import Assessment, Configuration
from src.app.services.edge_v2_service import digest
from tests.test_edge_section_results import judgment, replace_path, section_assessment
from tests.test_edge_section_results import section_receiver as section_receiver
from tests.test_edge_v2 import configuration, scan, send

URL = "/api/edge/v2/devices/site-demo/edge-demo/filtered-point-clouds"


def settings():
    return {
        "enabled": True,
        "processing_version": "ANGULAR_RANGE_MEDIAN_V1",
        "quality_min": 7,
        "distance_min_mm": 2,
        "distance_max_mm": 12000,
        "spatial_radius_points": 1,
        "spatial_min_neighbors": 2,
        "spatial_max_deviation_mm": 80,
        "spatial_neighbor_spread_mm": 40,
        "max_adjacent_angle_gap_mdeg": 400,
        "angle_match_tolerance_mdeg": 30,
    }


def cloud_state():
    body = section_assessment()
    clouds = []
    for suffix in ["a", "b"]:
        sid = "lidar-" + suffix
        ref = next(r for r in body["evidence"]["scans"] if r["sensor_id"] == sid)
        anchor = scan(suffix)
        anchor.pop("points")
        anchor.update(source_point_count=12, selected_point_count=2)
        clouds.append(
            {
                "sensor_id": sid,
                "state": "LIMITED",
                "reason_codes": ["DEFAULT_SETTINGS"],
                "coordinate_frame_id": ref["coordinate_frame_id"],
                "calibration_version": ref["calibration_version"],
                "verification_state": "DEFAULT",
                "processing_version": "ANGULAR_RANGE_MEDIAN_V1",
                "anchor": anchor,
                "source_scans": [copy.deepcopy(ref)],
                "point_count": 2,
                "valid_point_count": 1,
                "point_indices": [2, 7],
                "angles_mdeg": [10000, 20000],
                "xyz_mm": [[0, 0, 0], None],
                "valid": [True, False],
                "invalid_reason_codes": [[], ["LOW_QUALITY"]],
                "sample_counts": [1, 0],
                "processing": {
                    **settings(),
                    "window": {
                        "scan_count": 2,
                        "min_scans": 1,
                        "max_age_ms": 800,
                        "pair_tolerance_ms": 50,
                    },
                },
            }
        )
    body["filtered_point_clouds"] = clouds
    return body


@pytest.mark.parametrize("compact", [False, True])
async def test_cloud_is_committed_replayed_and_read_without_overwriting_raw(
    client, test_engine, monkeypatch, compact
):
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", str(compact).lower())
    get_settings.cache_clear()
    assert (await client.get(URL)).status_code == 401
    body = cloud_state()
    reply = await send(client, "/assessments", body)
    assert reply.status_code == 201, reply.text
    assert (await send(client, "/assessments", body)).json()["duplicate"] is True
    async with async_sessionmaker(test_engine)() as db:
        artifact = await db.scalar(
            select(EdgeV2Artifact).where(EdgeV2Artifact.kind == "assessment_revision")
        )
        expected = Assessment.model_validate(body).model_dump(mode="json", exclude={"message_id"})
        assert artifact.content_hash == digest(expected)
    assert (
        await client.post(
            "/api/v1/session", json={"username": "operator", "password": "operator1234"}
        )
    ).status_code == 200
    response = await client.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["field_present"] is True
    assert result["filtered_point_clouds"] == expected["filtered_point_clouds"]
    changed = copy.deepcopy(body)
    changed["message_id"] = "other-id"
    changed["filtered_point_clouds"][0]["xyz_mm"][0][2] = 0.125
    assert (await send(client, "/assessments", changed)).status_code == 409
    changed["message_id"] = body["message_id"]
    assert (await send(client, "/assessments", changed)).status_code == 409
    assert (await client.get(URL)).json() == result


@pytest.mark.parametrize("mode", ["missing", "null", "no_scan"])
async def test_absent_null_and_empty_sensor_arrays_are_distinct(client, mode):
    body = cloud_state()
    if mode == "missing":
        body.pop("filtered_point_clouds")
    elif mode == "null":
        body["filtered_point_clouds"] = None
    else:
        for c in body["filtered_point_clouds"]:
            c.update(
                state="UNAVAILABLE",
                reason_codes=["NO_SCAN"],
                anchor=None,
                source_scans=[],
                point_count=0,
                valid_point_count=0,
            )
            for key in [
                "point_indices",
                "angles_mdeg",
                "xyz_mm",
                "valid",
                "invalid_reason_codes",
                "sample_counts",
            ]:
                c[key] = []
    assert (await send(client, "/assessments", body)).status_code == 201
    await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    result = (await client.get(URL)).json()
    assert result["field_present"] == (mode != "missing")
    assert result["filtered_point_clouds"] == body.get("filtered_point_clouds")


@pytest.mark.parametrize(
    "path,value",
    [
        ("valid", [True]),
        ("point_indices", [2, 2]),
        ("point_indices", [2, 12]),
        ("angles_mdeg", [True, 20000]),
        ("sample_counts", [0, 0]),
        ("xyz_mm", [[float("nan"), 0, 0], None]),
        ("xyz_mm", [[0, 0, 0], [0, 0, 0]]),
        ("valid_point_count", 2),
        ("state", "AVAILABLE"),
        ("source_scans.0.config_revision", "wrong"),
        ("source_scans.0.calibration_version", "wrong"),
        ("source_scans", []),
        ("anchor.scan_id", "wrong"),
        ("anchor.acquired_at", 123),
        ("processing.window.min_scans", 3),
        ("processing.enabled", False),
        ("processing.spatial_radius_points", 1.0),
        ("invalid_reason_codes", [[], ["BOGUS"]]),
        ("sensor_id", "lidar-b"),
        ("point_indices", [True, 7]),
        ("xyz_mm", [[True, 0, 0], None]),
        ("point_count", 2.0),
        ("point_count", 3),
        ("anchor", None),
        ("anchor.selected_point_count", 1),
        ("angles_mdeg", [360000, 20000]),
        ("sample_counts", [2, 0]),
        ("verification_state", "UNVERIFIED"),
        ("calibration_version", None),
        ("source_scans.0.coordinate_frame_id", "wrong-frame"),
        ("processing.window.max_age_ms", 1001),
        ("processing.spatial_min_neighbors", 3),
    ],
)
def test_invalid_cloud_contract_is_rejected(path, value):
    body = cloud_state()
    replace_path(body, "filtered_point_clouds.0." + path, value)
    with pytest.raises(ValidationError):
        Assessment.model_validate(body)


@pytest.mark.parametrize("kind", ["configuration", "assessment"])
async def test_old_extended_artifact_hash_survives_cloud_upgrade(client, test_engine, kind):
    body = configuration() if kind == "configuration" else section_assessment()
    if kind == "configuration":
        body["judgment"] = judgment()
        model = Configuration.model_validate(body)
        content = model.model_dump(mode="json", include={"coordinate_frame", "sensors", "judgment"})
        content["judgment"].pop("point_cloud", None)
        key = [body["config_revision"]]
    else:
        content = Assessment.model_validate(body).model_dump(
            mode="json", exclude={"message_id", "filtered_point_clouds"}
        )
        key = [body["assessment_id"], body["revision"]]
    async with async_sessionmaker(test_engine)() as db:
        db.add(
            EdgeV2Artifact(
                site_id=body["site_id"],
                edge_id=body["edge_id"],
                kind="configuration" if kind == "configuration" else "assessment_revision",
                key_hash=digest(key),
                artifact_key=key,
                payload=content,
                content_hash=digest(content),
            )
        )
        await db.commit()
    response = await send(
        client, "/configurations" if kind == "configuration" else "/assessments", body
    )
    assert response.status_code == 201, response.text


async def test_new_configuration_settings_are_saved_and_immutable(client, test_engine):
    body = configuration()
    body["judgment"] = judgment()
    body["judgment"]["point_cloud"] = settings()
    response = await send(client, "/configurations", body)
    assert response.status_code == 201, response.text
    assert (await send(client, "/configurations", body)).json()["duplicate"] is True
    async with async_sessionmaker(test_engine)() as db:
        row = await db.scalar(
            select(EdgeV2Message).where(EdgeV2Message.message_type == "edge_configuration")
        )
        assert row.payload["judgment"]["point_cloud"] == settings()
    body["message_id"] = "new-settings-id"
    body["judgment"]["point_cloud"]["quality_min"] = 8
    assert (await send(client, "/configurations", body)).status_code == 409


@pytest.mark.parametrize(
    "path,value",
    [
        ("enabled", 1),
        ("quality_min", 256),
        ("spatial_radius_points", 1.0),
        ("distance_min_mm", 13000),
        ("spatial_min_neighbors", 3),
    ],
)
def test_point_cloud_settings_reject_invalid_types_and_bounds(path, value):
    body = configuration()
    body["judgment"] = judgment()
    body["judgment"]["point_cloud"] = settings()
    replace_path(body, "judgment.point_cloud." + path, value)
    with pytest.raises(ValidationError):
        Configuration.model_validate(body)


def test_point_cloud_settings_null_is_not_an_omission():
    body = configuration()
    body["judgment"] = judgment()
    assert (
        "point_cloud" not in Configuration.model_validate(body).model_dump(mode="json")["judgment"]
    )
    body["judgment"]["point_cloud"] = None
    with pytest.raises(ValidationError):
        Configuration.model_validate(body)


async def test_empty_read_large_cloud_and_late_state_do_not_invent_current_data(client):
    await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    before = (await client.get(URL)).json()
    assert before["message_id"] is None and before["field_present"] is False
    assert (await client.get(URL.replace("edge-demo", "unknown"))).status_code == 404
    body = cloud_state()
    for c in body["filtered_point_clouds"]:
        c.update(
            point_count=800,
            valid_point_count=800,
            point_indices=list(range(800)),
            angles_mdeg=list(range(800)),
            xyz_mm=[[1.125, 2.25, 3.375] for _ in range(800)],
            valid=[True] * 800,
            invalid_reason_codes=[[] for _ in range(800)],
            sample_counts=[1] * 800,
        )
        c["anchor"].update(source_point_count=800, selected_point_count=800)
    reply = await send(client, "/assessments", body)
    assert reply.status_code == 201, reply.text
    result = (await client.get(URL)).json()
    assert result["filtered_point_clouds"][0]["point_count"] == 800
    assert result["filtered_point_clouds"][1]["xyz_mm"][-1] == [1.125, 2.25, 3.375]
    old = copy.deepcopy(body)
    old.update(
        message_id="late-state",
        assessment_id="late-assessment",
        generated_at="2026-09-20T01:00:00Z",
    )
    assert (await send(client, "/assessments", old)).status_code == 201
    assert (await client.get(URL)).json() == result
