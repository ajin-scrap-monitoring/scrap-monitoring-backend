"""Synthetic independent heights: bounded input, durable provenance, and upgrade-safe hashes."""

import copy
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Assessment, Configuration
from src.app.services.edge_v2_service import digest
from tests.test_edge_v2 import DEVICES, assessment, configuration, send


@pytest.fixture(autouse=True)
def section_receiver(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps(DEVICES))
    monkeypatch.setenv("EDGE_V2_HEIGHT_FILL_ENABLED", "false")
    monkeypatch.setenv("EDGE_V2_HEIGHT_MEDIAN_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def judgment():
    return {
        "model_type": "INDEPENDENT_SECTIONS_V1",
        "method_version": "synthetic-sections-v1",
        "verification_state": "DEFAULT",
        "sections": [
            {
                "sensor_id": sid,
                "origin_xy_mm": [0, 0],
                "axis_xy": [1, 0] if sid == "lidar-a" else [0, 1],
                "bin_width_mm": 50,
                "floor_z_mm": [10, 10],
                "ceiling_z_mm": [110, 110],
                "active_bins": [True, True],
                "filters": {
                    "quality_min": 5,
                    "distance_min_mm": 1,
                    "distance_max_mm": 10000,
                    "height_margin_mm": 0,
                    "spatial_radius_bins": 0,
                    "spatial_max_deviation_mm": 10,
                },
                "max_interpolation_gap_bins": 1,
            }
            for sid in ["lidar-a", "lidar-b"]
        ],
        "window": {"scan_count": 3, "min_scans": 2, "max_age_ms": 900, "pair_tolerance_ms": 80},
        "fusion": {
            "method": "CALIBRATED_WEIGHTED_SECTIONS",
            "version": "synthetic-fusion-v1",
            "verification_state": "DEFAULT",
            "components": [
                {"sensor_id": sid, "weight": 0.5, "calibration": [[0, 0], [1, 1]]}
                for sid in ["lidar-a", "lidar-b"]
            ],
        },
    }


def section(sid, height):
    return {
        "sensor_id": sid,
        "state": "LIMITED",
        "reason_codes": ["DEFAULT_SETTINGS"],
        "profile": {
            "coordinate_frame_id": "frame-demo",
            "origin_xy_mm": [0, 0],
            "axis_xy": [1, 0] if sid == "lidar-a" else [0, 1],
            "plane_origin_mm": [0, 0, 120],
            "plane_normal": [0, 1, 0] if sid == "lidar-a" else [1, 0, 0],
            "area_basis": "VERTICAL_PROJECTION",
            "bin_width_mm": 50,
            "bins": [
                {
                    "index": i,
                    "floor_z_mm": 10,
                    "ceiling_z_mm": 110,
                    "height_mm": height,
                    "source": "OBSERVED",
                    "sensor_ids": [sid],
                }
                for i in range(2)
            ],
            "area_mm2": height * 100,
            "capacity_area_mm2": 10000,
            "area_ratio": height / 100,
            "coverage_ratio": 1,
            "sensor_stats": [
                {
                    "sensor_id": sid,
                    "scan_count": 2,
                    "input_points": 8,
                    "retained_points": 4,
                    "spatial_rejected_bins": 0,
                }
            ],
        },
    }


def section_assessment():
    body = assessment()
    body.update(
        rule_version="synthetic-sections-v1",
        decision_state="LIMITED",
        reason_codes=["DEFAULT_SETTINGS"],
        fill_state="NORMAL",
        fill_estimate={"ratio": 0.375, "method_version": "synthetic-fusion-v1"},
        section_profiles=[section("lidar-a", 0), section("lidar-b", 75)],
        fusion_result={
            "method": "CALIBRATED_WEIGHTED_SECTIONS",
            "model_version": "synthetic-fusion-v1",
            "ratio": 0.375,
            "pair_delta_ms": 1,
            "clock_domain_id": "synthetic-boot",
            "components": [
                {
                    "sensor_id": sid,
                    "section_ratio": value,
                    "calibrated_ratio": value,
                    "weight": 0.5,
                    "anchor_scan_id": f"scan-{suffix}-1",
                }
                for sid, suffix, value in [("lidar-a", "a", 0), ("lidar-b", "b", 0.75)]
            ],
        },
    )
    body["capabilities"]["fill"] = "ENABLED"
    body["evidence"]["scans"].append(
        {**body["evidence"]["scans"][0], "sensor_id": "lidar-b", "scan_id": "scan-b-1"}
    )
    return body


def replace_path(body, path, value):
    keys = path.split(".")
    target = body
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    key = keys[-1]
    target[int(key) if isinstance(target, list) else key] = value


@pytest.mark.parametrize(
    "path,value",
    [
        ("sections.0.filters.quality_min", True),
        ("sections.0.bin_width_mm", "50"),
        ("sections.0.bin_width_mm", 1.0),
        ("sections.0.axis_xy", [0, 0]),
        ("sections.0.origin_xy_mm", [float("nan"), 0]),
        ("sections.0.active_bins", [True]),
        ("sections.0.active_bins", [0, 1]),
        ("sections.0.active_bins", [False, False]),
        ("sections.0.ceiling_z_mm", [10, 110]),
        ("sections.0.floor_z_mm", [10] * 513),
        ("sections.0.filters.distance_min_mm", 10001),
        ("sections.0.sensor_id", "lidar-b"),
        ("sections.0.sensor_id", "unknown"),
        ("sections.0.extra", 1),
        ("window.min_scans", 4),
        ("window.scan_count", 9),
        ("window.max_age_ms", 1001),
        ("fusion.components.0.weight", 0),
        ("fusion.components.0.weight", 0.6),
        ("fusion.components.0.weight", float("inf")),
        ("fusion.components.0.sensor_id", "lidar-b"),
        ("fusion.components.0.calibration", [[0.1, 0], [1, 1]]),
        ("fusion.components.0.calibration", [[0, 0.9], [1, 0.2]]),
        ("fusion.components.0.calibration", [[0, 0], [0, 0.5], [1, 1]]),
        ("verification_state", "NORMAL"),
    ],
)
def test_invalid_judgment_cannot_be_registered(path, value):
    body = configuration()
    body["judgment"] = judgment()
    replace_path(body["judgment"], path, value)
    with pytest.raises(ValidationError):
        Configuration.model_validate(body)


@pytest.mark.parametrize(
    "path,value",
    [
        ("section_profiles.0.profile.bins.0.height_mm", -1),
        ("section_profiles.0.profile.bins.0.height_mm", True),
        ("section_profiles.0.profile.bins.0.height_mm", 101),
        ("section_profiles.0.profile.bins.0.height_mm", None),
        ("section_profiles.0.profile.bins.0.source", "MASKED"),
        ("section_profiles.0.profile.bins.0.index", 1),
        ("section_profiles.0.profile.bins.0.sensor_ids", ["lidar-b"]),
        ("section_profiles.0.profile.sensor_stats.0.sensor_id", "lidar-b"),
        ("section_profiles.0.profile.sensor_stats.0.retained_points", 9),
        ("section_profiles.0.profile.area_mm2", 1),
        ("section_profiles.0.profile.capacity_area_mm2", 1),
        ("section_profiles.0.profile.coverage_ratio", 0.5),
        ("section_profiles.0.profile.plane_normal", [0, 0, 0]),
        ("section_profiles.0.profile.extra", 1),
        ("section_profiles.0.sensor_id", "lidar-b"),
        ("section_profiles.0.state", "AVAILABLE"),
        ("section_profiles.0.reason_codes", ["DEFAULT_SETTINGS", "DEFAULT_SETTINGS"]),
        ("fusion_result.components.0.anchor_scan_id", "missing"),
        ("fusion_result.components.0.sensor_id", "lidar-b"),
        ("fusion_result.components.0.section_ratio", 0.1),
        ("fusion_result.components.0.weight", 0.4),
        ("fusion_result.pair_delta_ms", 101),
        ("fusion_result.ratio", 0.5),
        ("fusion_result", None),
        ("fill_estimate.ratio", 0.5),
        ("fill_estimate.method_version", "other-model"),
        ("decision_state", "AVAILABLE"),
        ("reason_codes", []),
        ("evidence.scans.0.config_revision", "other-config"),
        ("evidence.scans.0.coordinate_frame_id", "other-frame"),
    ],
)
def test_inconsistent_heights_or_fusion_are_rejected(path, value):
    body = section_assessment()
    replace_path(body, path, value)
    with pytest.raises(ValidationError):
        Assessment.model_validate(body)


@pytest.mark.parametrize("compact", [False, True])
async def test_extended_bodies_commit_before_ack_and_replay(
    client, test_engine, monkeypatch, compact
):
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", str(compact).lower())
    get_settings.cache_clear()
    body = section_assessment()
    # Judgments can arrive before their configuration and scan bodies, like existing v2 data.
    response = await send(client, "/assessments", body)
    assert response.status_code == 201, response.text
    replay = await send(client, "/assessments", copy.deepcopy(body))
    assert replay.json() == {**response.json(), "duplicate": True}
    config = configuration()
    config["judgment"] = judgment()
    assert (await send(client, "/configurations", config)).status_code == 201
    async with async_sessionmaker(test_engine)() as db:
        config_row = await db.scalar(
            select(EdgeV2Message).where(EdgeV2Message.message_type == "edge_configuration")
        )
        assert config_row.payload["judgment"] == config["judgment"]
        row = await db.scalar(
            select(EdgeV2Message).where(EdgeV2Message.message_type == "edge_assessment")
        )
        if compact:
            assert "section_profiles" not in row.payload
            latest = await db.get(EdgeV2Latest, (body["site_id"], body["edge_id"], "state"))
            assert latest.payload == body
        else:
            assert row.payload == body
        artifact = await db.scalar(
            select(EdgeV2Artifact).where(EdgeV2Artifact.kind == "configuration")
        )
        assert artifact.payload["judgment"] == config["judgment"]
    altered = copy.deepcopy(config)
    altered["message_id"] = "same-revision-new-judgment"
    altered["judgment"]["window"]["max_age_ms"] = 800
    assert (await send(client, "/configurations", altered)).status_code == 409


@pytest.mark.parametrize("mode", ["missing", "incomplete", "interpolated", "masked", "time-skew"])
async def test_partial_sections_keep_null_zero_and_provenance(client, test_engine, mode):
    body = section_assessment()
    body.update(fusion_result=None, fill_estimate=None, fill_state="UNAVAILABLE")
    second = body["section_profiles"][1]
    if mode == "missing":
        second.update(profile=None, state="UNAVAILABLE", reason_codes=["INSUFFICIENT_HISTORY"])
    elif mode == "time-skew":
        body["reason_codes"].append("SENSOR_TIME_MISMATCH")
    else:
        profile = second["profile"]
        item = profile["bins"][1]
        item["sensor_ids"] = []
        item["source"] = {
            "incomplete": "UNAVAILABLE",
            "interpolated": "INTERPOLATED",
            "masked": "MASKED",
        }[mode]
        profile["coverage_ratio"] = 0.5
        if mode == "incomplete":
            item["height_mm"] = None
            profile.update(area_mm2=None, area_ratio=None)
            second.update(state="UNAVAILABLE", reason_codes=["SECTION_INCOMPLETE"])
        elif mode == "masked":
            item["height_mm"] = None
            profile.update(capacity_area_mm2=5000, area_mm2=3750, coverage_ratio=1)
        else:
            second["reason_codes"].append("INTERPOLATED_BINS")
    response = await send(client, "/assessments", body)
    assert response.status_code == 201, response.text
    async with async_sessionmaker(test_engine)() as db:
        saved = await db.scalar(select(EdgeV2Message))
        assert saved.payload == body
        assert saved.payload["section_profiles"][0]["profile"]["bins"][0]["height_mm"] == 0


@pytest.mark.parametrize("kind", ["configuration", "assessment"])
@pytest.mark.parametrize("stored_hash", [False, True])
async def test_pre_extension_artifact_hashes_remain_compatible(
    client, test_engine, kind, stored_hash
):
    body = configuration() if kind == "configuration" else assessment()
    content = (
        {k: body[k] for k in ["coordinate_frame", "sensors"]}
        if kind == "configuration"
        else {k: v for k, v in body.items() if k != "message_id"}
    )
    # Pre-extension artifacts used Pydantic's numeric and timestamp serialization,
    # rather than the original HTTP body's integer/three-digit forms.
    if kind == "configuration":
        for sensor in content["sensors"]:
            cal = sensor["calibration"]
            cal["rotation"] = [[float(x) for x in row] for row in cal["rotation"]]
            cal["translation_mm"] = [float(x) for x in cal["translation_mm"]]
    else:
        content["generated_at"] = "2026-09-20T01:00:01Z"
        content["observed_from"] = content["observed_to"] = "2026-09-20T01:00:00.950000Z"
    key = [body["config_revision"]] if kind == "configuration" else [body["assessment_id"], 1]
    async with async_sessionmaker(test_engine)() as db:
        db.add(
            EdgeV2Artifact(
                site_id=body["site_id"],
                edge_id=body["edge_id"],
                kind="configuration" if kind == "configuration" else "assessment_revision",
                key_hash=digest(key),
                artifact_key=key,
                payload=content,
                content_hash=digest(content) if stored_hash else None,
            )
        )
        await db.commit()
    response = await send(
        client, "/configurations" if kind == "configuration" else "/assessments", body
    )
    assert response.status_code == 201, response.text


async def test_authenticated_sensor_scope_rejects_foreign_sections(client, test_engine):
    body = section_assessment()
    body.update(fill_estimate=None, fusion_result=None, fill_state="UNAVAILABLE")
    body["section_profiles"][1] = {
        "sensor_id": "foreign",
        "state": "UNAVAILABLE",
        "reason_codes": [],
        "profile": None,
    }
    response = await send(client, "/assessments", body)
    assert response.status_code == 422
    async with async_sessionmaker(test_engine)() as db:
        assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 0


def test_disabled_settings_and_legacy_messages_still_validate():
    config = configuration()
    assert Configuration.model_validate(config).judgment is None
    config["judgment"] = None
    assert Configuration.model_validate(config).judgment is None
    assert Assessment.model_validate(assessment()).section_profiles is None
    config["judgment"] = judgment()
    config["judgment"]["verification_state"] = "UNVERIFIED"
    config["judgment"]["fusion"] = None
    assert Configuration.model_validate(config).judgment.fusion is None


def test_plane_normal_keeps_the_existing_calibration_rounding_tolerance():
    body = section_assessment()
    body["section_profiles"][0]["profile"]["plane_normal"] = [0, 0.9999999, 0]
    assert Assessment.model_validate(body).section_profiles[0].profile is not None


async def test_changed_assessment_cannot_overwrite_receipt_or_revision(client, test_engine):
    body = section_assessment()
    assert (await send(client, "/assessments", body)).status_code == 201
    body["reason_codes"].append("ADDITIONAL_REASON")
    response = await send(client, "/assessments", body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    body["message_id"] = "new-envelope-same-assessment"
    response = await send(client, "/assessments", body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACT_CONFLICT"
    async with async_sessionmaker(test_engine)() as db:
        assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 1
        row = await db.scalar(select(EdgeV2Message))
        assert row.payload["reason_codes"] == ["DEFAULT_SETTINGS"]


async def test_new_revision_can_change_or_disable_judgment(client, test_engine):
    body = configuration()
    assert (await send(client, "/configurations", body)).status_code == 201
    body.update(message_id="explicit-disabled", judgment=None)
    # Absent and explicit null describe the same immutable disabled configuration.
    assert (await send(client, "/configurations", body)).status_code == 201
    body.update(message_id="new-config", config_revision="cfg-2", judgment=judgment())
    assert (await send(client, "/configurations", body)).status_code == 201
    async with async_sessionmaker(test_engine)() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(EdgeV2Artifact)
                .where(EdgeV2Artifact.kind == "configuration")
            )
            == 2
        )


@pytest.mark.parametrize("event", [False, True])
async def test_compact_retention_preserves_extended_event_or_latest_state(
    client, test_engine, monkeypatch, event
):
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    body = section_assessment()
    if event:
        body.update(assessment_type="EVENT", event={"kind": "FILL_INCREASE", "stage": "CONFIRMED"})
    assert (await send(client, "/assessments", body)).status_code == 201
    newer = copy.deepcopy(body)
    newer.update(
        message_id="newer-assessment",
        assessment_id="assessment-2",
        generated_at="2026-09-20T01:00:02.000Z",
    )
    assert (await send(client, "/assessments", newer)).status_code == 201
    async with async_sessionmaker(test_engine)() as db:
        rows = list((await db.scalars(select(EdgeV2Message).order_by(EdgeV2Message.id))).all())
        if event:
            assert rows[0].payload["section_profiles"] == body["section_profiles"]
            artifact = await db.scalar(
                select(EdgeV2Artifact).where(EdgeV2Artifact.kind == "assessment_revision")
            )
            assert artifact.payload["fusion_result"] == body["fusion_result"]
        else:
            assert all("section_profiles" not in r.payload for r in rows)
            latest = await db.get(EdgeV2Latest, (body["site_id"], body["edge_id"], "state"))
            assert latest.message_id == newer["message_id"]
            assert latest.payload == newer
            assert await db.scalar(select(func.count()).select_from(EdgeV2Latest)) == 1


async def test_invalid_extended_body_is_not_acknowledged_or_stored(client, test_engine):
    body = section_assessment()
    body["section_profiles"][0]["profile"]["bins"][0]["height_mm"] = "0"
    response = await send(client, "/assessments", body)
    assert response.status_code == 422
    assert response.json()["accepted"] is False
    async with async_sessionmaker(test_engine)() as db:
        assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 0
        assert await db.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 0
