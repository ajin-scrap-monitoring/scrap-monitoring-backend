"""Synthetic v2 contract tests, durable receipts, and independent-writer races."""

import asyncio
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import Settings, get_settings
from src.app.db.base import Base
from src.app.db.session import get_db_session
from src.app.main import create_app
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.metric import ScrapMetric
from src.app.services.edge_v2_service import device_status, utc

BASE = "/api/edge/v2"
TOKEN = "synthetic-v2-device-token"
TIME = "2026-09-20T01:00:01.000Z"
DEVICES = [
    {
        "site_id": "site-demo",
        "edge_id": "edge-demo",
        "sensor_ids": ["lidar-a", "lidar-b"],
        "token": TOKEN,
    },
    {
        "site_id": "site-other",
        "edge_id": "edge-other",
        "sensor_ids": ["lidar-a", "lidar-b"],
        "token": "synthetic-other-token",
    },
]


@pytest.fixture(autouse=True)
def v2_credentials(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps(DEVICES))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def envelope(kind, message_id):
    return {
        "schema_version": "2.0",
        "message_type": kind,
        "message_id": message_id,
        "site_id": "site-demo",
        "edge_id": "edge-demo",
        "generated_at": TIME,
        "config_revision": "cfg-1",
    }


def scan(sensor="a", offset=0, count=1):
    return {
        "scan_id": f"scan-{sensor}-1",
        "stream_instance_id": f"stream-{sensor}",
        "sequence": 1,
        "acquired_at": "2026-09-20T01:00:00.950Z",
        "timestamp_source": "EDGE_SCAN_RECEIVED",
        "clock_domain_id": "boot-1",
        "acquired_monotonic_ns": str(1000950000000 + offset),
        "clock_state": "SYNCED",
        "clock_offset_ms": 8,
        "source_point_count": count,
        "selected_point_count": count,
        "points": [
            {
                "index": i,
                "angle_mdeg": i * 100,
                "distance_mm": 1000 + i,
                "quality": 47,
                "position_mm": None,
                "calculation_reason_codes": [],
            }
            for i in range(count)
        ],
    }


def sensor(name="a", present=True, count=1):
    return {
        "sensor_id": f"lidar-{name}",
        "availability": "PRESENT" if present else "NO_NEW_SCAN",
        "calibration_version": f"cal-{name}-1",
        "transform_state": "UNVERIFIED",
        "transform_reason_codes": ["CALIBRATION_UNVERIFIED"],
        "angle_intervals_mdeg": [{"start_inclusive": 0, "end_exclusive": 180000}],
        "scan": scan(name, count=count) if present else None,
    }


def measurement(message_id="m-1", count=1):
    return {
        **envelope("lidar_measurement", message_id),
        "delivery_kind": "PERIODIC",
        "report_interval_ms": 1000,
        "selection_policy": "LATEST_TIME_MATCHED_PAIR",
        "coordinate_frame_id": "frame-demo",
        "pair_state": "PARTIAL",
        "pair_delta_ms": None,
        "sensors": [sensor(count=count), sensor("b", present=False)],
    }


def assessment(message_id="a-1"):
    return {
        **envelope("edge_assessment", message_id),
        "assessment_type": "STATE",
        "assessment_id": "assessment-1",
        "revision": 1,
        "observed_from": "2026-09-20T01:00:00.950Z",
        "observed_to": "2026-09-20T01:00:00.950Z",
        "rule_version": None,
        "decision_state": "UNAVAILABLE",
        "reason_codes": ["FILL_METHOD_NOT_VALIDATED"],
        "fill_state": "UNAVAILABLE",
        "fill_estimate": None,
        "event": None,
        "capabilities": {
            "fill": "NOT_VALIDATED",
            "occlusion": "NOT_IMPLEMENTED",
            "collection": "NOT_IMPLEMENTED",
            "vision": "NOT_IMPLEMENTED",
        },
        "evidence": {
            "scans": [
                {
                    "scan_id": "scan-a-1",
                    "sensor_id": "lidar-a",
                    "config_revision": "cfg-1",
                    "calibration_version": "cal-a-1",
                    "coordinate_frame_id": "frame-demo",
                }
            ],
            "vision_observations": [],
        },
    }


def status_report(message_id="s-1", sequence=1):
    return {
        **envelope("edge_status", message_id),
        "orchestrator_instance_id": "orch-1",
        "sequence": sequence,
        "overall_state": "DEGRADED",
        "reason_codes": ["SCAN_NOT_UPDATED"],
        "clock": {"state": "SYNCED", "offset_ms": 8},
        "sensors": [
            {
                "sensor_id": "lidar-a",
                "data_state": "FRESH",
                "last_scan_at": TIME,
                "reason_codes": [],
            },
            {
                "sensor_id": "lidar-b",
                "data_state": "STALE",
                "last_scan_at": None,
                "reason_codes": ["SCAN_NOT_UPDATED"],
            },
        ],
        "services": [
            {
                "service": "lidar-processing",
                "state": "HEALTHY",
                "last_progress_at": TIME,
                "reason_codes": [],
            }
        ],
        "delivery": {
            "pending_messages": 20,
            "pending_bytes": 1000000,
            "quarantined_messages": 0,
            "last_backend_ack_at": None,
        },
        "storage": {
            "limit_bytes": 4294967296,
            "used_bytes": 2000000,
            "lost_messages": 0,
            "loss_from": None,
            "loss_to": None,
            "last_loss_reason": None,
            "unavailable_evidence_scan_ids": [],
        },
    }


def configuration(message_id="c-1"):
    return {
        **envelope("edge_configuration", message_id),
        "coordinate_frame": {
            "frame_id": "frame-demo",
            "unit": "mm",
            "origin_description": "synthetic origin",
            "x_axis_description": "x",
            "y_axis_description": "y",
            "z_axis_description": "z",
            "handedness": "RIGHT_HANDED",
        },
        "sensors": [
            {
                "sensor_id": f"lidar-{name}",
                "angle_intervals_mdeg": [{"start_inclusive": 0, "end_exclusive": 180000}],
                "calibration": {
                    "version": f"cal-{name}-1",
                    "verification_state": "UNVERIFIED",
                    "angle_sign": -1,
                    "angle_zero_offset_mdeg": 0,
                    "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    "translation_mm": [0, 0, 0],
                },
            }
            for name in ("a", "b")
        ],
    }


async def send(client, path, payload, token=TOKEN, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": payload["message_id"]}
    headers.update(kwargs.pop("headers", {}))
    return await client.post(BASE + path, json=payload, headers=headers, **kwargs)


@pytest.mark.parametrize(
    "path,factory",
    [
        ("/measurements", measurement),
        ("/assessments", assessment),
        ("/status", status_report),
        ("/configurations", configuration),
    ],
)
async def test_all_receivers_commit_before_ack_and_replay(client, test_engine, path, factory):
    payload = factory()
    first = await send(client, path, payload)
    assert first.status_code == 201, first.text
    ack = first.json()
    assert ack["accepted"] is True and ack["duplicate"] is False
    assert ack["message_id"] == payload["message_id"]
    assert datetime.fromisoformat(ack["received_at"]).utcoffset() == timedelta(0)
    # A separate session sees the committed payload before the sender discards its outbox entry.
    async with async_sessionmaker(test_engine)() as db:
        row = await db.scalar(select(EdgeV2Message))
        assert row.payload == payload
    # Simulate lost ACK: the caller replays identical JSON with different key order.
    replay = await send(client, path, dict(reversed(list(payload.items()))))
    assert replay.status_code == 200
    assert replay.json() == {**ack, "duplicate": True}


async def test_idempotency_spans_apis_and_device_scopes(client, test_db_session):
    original = measurement()
    assert (await send(client, "/measurements", original)).status_code == 201
    changed = copy.deepcopy(original)
    changed["sensors"][0]["scan"]["points"][0]["distance_mm"] += 1
    response = await send(client, "/measurements", changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    collision = assessment(original["message_id"])
    assert (await send(client, "/assessments", collision)).status_code == 409
    other = copy.deepcopy(original)
    other.update(site_id="site-other", edge_id="edge-other")
    assert (await send(client, "/measurements", other, "synthetic-other-token")).status_code == 201
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Message)) == 2


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({}, 401),
        ({"X-Edge-API-Key": TOKEN}, 401),
        ({"Authorization": TOKEN}, 401),
        ({"Authorization": "Basic " + TOKEN}, 401),
        ({"Authorization": "Bearer wrong-token"}, 401),
        ({"Authorization": "Bearer synthetic-other-token"}, 403),
    ],
)
async def test_authentication_and_scope(client, headers, expected):
    payload = measurement()
    response = await client.post(
        BASE + "/measurements", json=payload, headers={"Idempotency-Key": "m-1", **headers}
    )
    assert response.status_code == expected
    assert response.json()["accepted"] is False
    assert TOKEN not in response.text


async def test_transport_headers_and_safe_validation_errors(client):
    payload = measurement()
    for headers in ({"Idempotency-Key": "different"}, {"Idempotency-Key": ""}):
        response = await send(client, "/measurements", payload, headers=headers)
        assert response.status_code == 422
        assert response.json()["error"]["field"] == "Idempotency-Key"
    for headers in ({"Content-Type": "text/plain"}, {"Content-Encoding": "gzip"}):
        assert (await send(client, "/measurements", payload, headers=headers)).status_code == 415
    invalid = await client.post(
        BASE + "/measurements",
        content='{ "message_id": ',
        headers={"Content-Type": "application/json"},
    )
    assert invalid.status_code == 422 and invalid.json()["message_id"] is None
    payload[TOKEN] = ["synthetic raw data"] * 1000
    response = await send(client, "/measurements", payload)
    assert response.status_code == 422
    assert response.json()["message_id"] == "m-1"
    assert TOKEN not in response.text and "synthetic raw data" not in response.text
    assert len(response.content) < 300


async def test_non_utf8_and_missing_idempotency_header_are_rejected(client):
    payload = measurement()
    encoded = json.dumps(payload).encode("utf-16-le")
    response = await client.post(
        BASE + "/measurements",
        content=encoded,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + TOKEN,
            "Idempotency-Key": "m-1",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCHEMA_VALIDATION_FAILED"
    missing = await client.post(
        BASE + "/measurements", json=payload, headers={"Authorization": "Bearer " + TOKEN}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["field"] == "Idempotency-Key"


async def test_device_token_rotation_and_revocation(client, monkeypatch):
    first = await send(client, "/measurements", measurement())
    assert first.status_code == 201
    rotated = copy.deepcopy(DEVICES)
    rotated.append({**DEVICES[0], "token": "synthetic-rotated-token"})
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps(rotated))
    get_settings.cache_clear()
    replay = await send(client, "/measurements", measurement(), "synthetic-rotated-token")
    assert replay.status_code == 200 and replay.json()["received_at"] == first.json()["received_at"]
    rotated.pop(0)
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps(rotated))
    get_settings.cache_clear()
    assert (await send(client, "/measurements", measurement())).status_code == 401
    assert (
        await send(client, "/measurements", measurement(), "synthetic-rotated-token")
    ).status_code == 200


@pytest.mark.parametrize(
    "case",
    [
        "point_count",
        "fractional_period",
        "duplicate_index",
        "index_range",
        "missing_slot",
        "unknown_sensor",
        "duplicate_sensor",
        "missing_version",
        "numeric_time",
        "numeric_string_time",
        "naive_time",
        "non_utc_time",
        "numeric_ns",
        "oversized_sequence",
        "string_distance",
        "boolean_distance",
        "unverified_position",
        "angle_outside",
        "old_scan",
        "bad_pair_state",
    ],
)
async def test_measurement_validation(client, case):
    payload = measurement(count=2)
    observation = payload["sensors"][0]
    raw_scan = observation["scan"]
    if case == "point_count":
        raw_scan["selected_point_count"] = 1
    elif case == "fractional_period":
        payload["report_interval_ms"] = 1000.0
    elif case == "duplicate_index":
        raw_scan["points"][1]["index"] = 0
    elif case == "index_range":
        raw_scan["points"][1]["index"] = 2
    elif case == "missing_slot":
        payload["sensors"].pop()
    elif case == "unknown_sensor":
        observation["sensor_id"] = "lidar-other"
    elif case == "duplicate_sensor":
        payload["sensors"][1]["sensor_id"] = "lidar-a"
    elif case == "missing_version":
        payload.pop("schema_version")
    elif case == "numeric_time":
        payload["generated_at"] = 1000000
    elif case == "numeric_string_time":
        payload["generated_at"] = "1000000"
    elif case == "naive_time":
        payload["generated_at"] = "2026-09-20T01:00:01"
    elif case == "non_utc_time":
        payload["generated_at"] = "2026-09-20T10:00:01+09:00"
    elif case == "numeric_ns":
        raw_scan["acquired_monotonic_ns"] = 1000950000000
    elif case == "oversized_sequence":
        raw_scan["sequence"] = 9007199254740992
    elif case == "string_distance":
        raw_scan["points"][0]["distance_mm"] = "1000"
    elif case == "boolean_distance":
        raw_scan["points"][0]["distance_mm"] = True
    elif case == "unverified_position":
        raw_scan["points"][0]["position_mm"] = {"x": 1, "y": 2, "z": 3}
    elif case == "angle_outside":
        raw_scan["points"][0]["angle_mdeg"] = 180000
    elif case == "old_scan":
        raw_scan["acquired_at"] = "2026-09-20T00:59:59Z"
    elif case == "bad_pair_state":
        payload["pair_state"] = "MATCHED"
    response = await send(client, "/measurements", payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "SCHEMA_VALIDATION_FAILED"


@pytest.mark.parametrize(
    "delta_ms,state,domain,valid",
    [
        (100, "MATCHED", "boot-1", True),
        (101, "UNMATCHED", "boot-1", True),
        (101, "MATCHED", "boot-1", False),
        (100, "UNMATCHED", "boot-1", False),
        (None, "UNMATCHED", "boot-other", True),
        (None, "MATCHED", "boot-other", False),
    ],
)
async def test_pair_matching_uses_monotonic_clock(client, delta_ms, state, domain, valid):
    payload = measurement()
    payload["sensors"][1] = sensor("b")
    payload["sensors"][1]["scan"]["clock_domain_id"] = domain
    payload["sensors"][1]["scan"]["acquired_monotonic_ns"] = str(
        1000950000000 + (delta_ms or 0) * 1000000
    )
    payload.update(pair_state=state, pair_delta_ms=delta_ms)
    response = await send(client, "/measurements", payload)
    assert response.status_code == (201 if valid else 422), response.text


async def test_empty_periodic_and_invalid_sdk_ranges_are_not_zero_fill(client, test_db_session):
    payload = measurement()
    payload.update(pair_state="NONE", sensors=[sensor("a", False), sensor("b", False)])
    assert (await send(client, "/measurements", payload)).status_code == 201
    invalid_range = measurement("m-2")
    point = invalid_range["sensors"][0]["scan"]["points"][0]
    point.update(distance_mm=0, quality=0, calculation_reason_codes=["SDK_INVALID_RANGE"])
    assert (await send(client, "/measurements", invalid_range)).status_code == 201
    assert await test_db_session.scalar(select(func.count()).select_from(ScrapMetric)) == 0


async def test_out_of_order_evidence_and_configuration_preserve_references(client, test_db_session):
    # Assessment arrives before both its scan and its configuration.
    assert (await send(client, "/assessments", assessment())).status_code == 201
    payload = measurement()
    payload.update(
        delivery_kind="EVIDENCE",
        report_interval_ms=None,
        selection_policy="REFERENCED_SCANS",
        pair_state="NOT_APPLICABLE",
        assessment_ids=["assessment-1"],
        generated_at="2026-09-21T01:00:00Z",
    )
    payload["sensors"] = payload["sensors"][:1]
    assert (await send(client, "/measurements", payload)).status_code == 201
    assert (await send(client, "/configurations", configuration())).status_code == 201
    stored = (await test_db_session.scalars(select(EdgeV2Message))).all()
    assert stored[0].payload["evidence"]["scans"][0]["scan_id"] == "scan-a-1"
    assert stored[1].payload["sensors"][0]["scan"]["acquired_at"].endswith("00.950Z")
    assert stored[0].payload["fill_estimate"] is None
    assert await test_db_session.scalar(select(func.count()).select_from(ScrapMetric)) == 0


async def test_scan_references_do_not_create_duplicate_observations(client, test_db_session):
    periodic = measurement()
    assert (await send(client, "/measurements", periodic)).status_code == 201
    evidence = copy.deepcopy(periodic)
    evidence.update(
        message_id="m-2",
        delivery_kind="EVIDENCE",
        report_interval_ms=None,
        selection_policy="REFERENCED_SCANS",
        pair_state="NOT_APPLICABLE",
        assessment_ids=["assessment-1"],
    )
    evidence["sensors"] = evidence["sensors"][:1]
    assert (await send(client, "/measurements", evidence)).status_code == 201
    sources = (
        select(func.count()).select_from(EdgeV2Artifact).where(EdgeV2Artifact.kind == "scan_source")
    )
    results = (
        select(func.count()).select_from(EdgeV2Artifact).where(EdgeV2Artifact.kind == "scan_result")
    )
    assert await test_db_session.scalar(sources) == 1
    assert await test_db_session.scalar(results) == 1
    changed = copy.deepcopy(evidence)
    changed.update(message_id="m-3", config_revision="cfg-2")
    assert (await send(client, "/measurements", changed)).status_code == 201
    assert await test_db_session.scalar(sources) == 1
    assert await test_db_session.scalar(results) == 2
    changed["message_id"] = "m-4"
    changed["sensors"][0]["scan"]["points"][0]["quality"] = 12
    response = await send(client, "/measurements", changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACT_CONFLICT"


@pytest.mark.parametrize(
    "case", ["configuration", "calibration", "rotation", "reflection", "boolean_sign"]
)
async def test_immutable_configurations_and_valid_rotations(client, case):
    payload = configuration()
    assert (await send(client, "/configurations", payload)).status_code == 201
    payload["message_id"] = "c-2"
    if case == "configuration":
        payload["coordinate_frame"]["origin_description"] = "changed origin"
    elif case == "calibration":
        payload["config_revision"] = "cfg-2"
        payload["sensors"][0]["calibration"]["translation_mm"][0] = 1
    elif case == "rotation":
        payload["sensors"][0]["calibration"]["rotation"][0][0] = 2
    elif case == "reflection":
        payload["sensors"][0]["calibration"]["rotation"][0][0] = -1
    elif case == "boolean_sign":
        payload["sensors"][0]["calibration"]["angle_sign"] = True
    response = await send(client, "/configurations", payload)
    assert response.status_code == (409 if case in ("configuration", "calibration") else 422)


async def test_event_revisions_can_arrive_in_reverse_order(client, test_db_session):
    newer = assessment()
    newer.update(
        assessment_type="EVENT", revision=2, event={"kind": "COLLECTION", "stage": "CONFIRMED"}
    )
    assert (await send(client, "/assessments", newer)).status_code == 201
    older = copy.deepcopy(newer)
    older.update(message_id="a-2", revision=1, event={"kind": "COLLECTION", "stage": "SUSPECTED"})
    assert (await send(client, "/assessments", older)).status_code == 201
    older["message_id"] = "a-3"
    older["event"]["stage"] = "RETRACTED"
    assert (await send(client, "/assessments", older)).status_code == 409
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Message)) == 2


async def test_unvalidated_fill_cannot_create_fake_estimate(client):
    payload = assessment()
    payload["fill_estimate"] = {"ratio": 0, "method_version": "not-validated"}
    assert (await send(client, "/assessments", payload)).status_code == 422
    payload.update(decision_state="AVAILABLE", fill_state="AVAILABLE", rule_version="rule-demo")
    payload["capabilities"]["fill"] = "ENABLED"
    payload["fill_estimate"]["ratio"] = 1.01
    assert (await send(client, "/assessments", payload)).status_code == 422
    payload["fill_estimate"]["ratio"] = 0.5
    assert (await send(client, "/assessments", payload)).status_code == 201


async def test_new_state_generation_requires_new_assessment_id(client):
    payload = assessment()
    assert (await send(client, "/assessments", payload)).status_code == 201
    payload.update(message_id="a-2", generated_at="2026-09-20T01:00:02Z")
    assert (await send(client, "/assessments", payload)).status_code == 409
    payload["assessment_id"] = "assessment-2"
    assert (await send(client, "/assessments", payload)).status_code == 201


async def test_status_receipt_timeout_sequence_and_authenticated_view(client, test_db_session):
    report = status_report(sequence=10)
    # Wall clock is historical, connectivity is based on server receipt time.
    assert (await send(client, "/status", report)).status_code == 201
    older = status_report("s-2", sequence=9)
    older.update(generated_at="2026-09-20T01:00:02Z", overall_state="HEALTHY")
    assert (await send(client, "/status", older)).status_code == 201
    path = BASE + "/devices/site-demo/edge-demo/status"
    assert (await client.get(path)).status_code == 401
    assert (
        await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    ).status_code == 200
    view = (await client.get(path)).json()
    assert view["snapshot"]["sequence"] == 10
    assert view["effective_state"] == "DEGRADED" and view["connection_state"] == "REPORTING"
    received = datetime.fromisoformat(view["last_received_at"])
    before = await device_status(
        test_db_session, "site-demo", "edge-demo", received + timedelta(seconds=29.999)
    )
    expired = await device_status(
        test_db_session, "site-demo", "edge-demo", received + timedelta(seconds=30)
    )
    assert before.connection_state == "REPORTING"
    assert expired.connection_state == "UNKNOWN" and expired.effective_state == "UNKNOWN"
    assert expired.snapshot.sensors[1].data_state == "STALE"
    replay = await send(client, "/status", older)
    assert replay.status_code == 200
    again = await device_status(
        test_db_session, "site-demo", "edge-demo", received + timedelta(seconds=30)
    )
    assert again.last_received_at == received and again.connection_state == "UNKNOWN"


async def test_new_status_instance_and_missing_status(client, test_db_session):
    missing = await device_status(test_db_session, "site-demo", "edge-demo")
    assert missing.snapshot is None and missing.effective_state == "UNKNOWN"
    assert (await send(client, "/status", status_report(sequence=99))).status_code == 201
    rebooted = status_report("s-2", sequence=0)
    rebooted.update(
        orchestrator_instance_id="orch-2",
        generated_at="2026-09-20T01:00:02Z",
        overall_state="HEALTHY",
    )
    assert (await send(client, "/status", rebooted)).status_code == 201
    current = await device_status(test_db_session, "site-demo", "edge-demo")
    assert current.snapshot.orchestrator_instance_id == "orch-2"
    assert current.snapshot.sequence == 0 and current.effective_state == "HEALTHY"


async def test_commit_failure_never_returns_accepted_ack(client, test_db_session, monkeypatch):
    original_commit = test_db_session.commit

    async def fail_commit():
        raise OperationalError("synthetic commit failure", None, Exception("synthetic"))

    monkeypatch.setattr(test_db_session, "commit", fail_commit)
    response = await send(client, "/measurements", measurement())
    assert response.status_code == 503
    assert response.json()["accepted"] is False and response.json()["error"]["retryable"] is True
    monkeypatch.setattr(test_db_session, "commit", original_commit)
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Message)) == 0
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 0
    assert (await send(client, "/measurements", measurement())).status_code == 201


async def test_full_scan_over_64_kib_is_preserved(client, test_db_session):
    payload = measurement(count=1440)
    raw_size = len(json.dumps(payload).encode())
    assert raw_size > 65536
    async with asyncio.timeout(5):
        response = await send(client, "/measurements", payload)
    assert response.status_code == 201, response.text
    row = await test_db_session.scalar(select(EdgeV2Message))
    assert len(row.payload["sensors"][0]["scan"]["points"]) == 1440
    assert row.payload["sensors"][0]["scan"]["points"] == payload["sensors"][0]["scan"]["points"]
    # Points are JSON arrays, not thousands of relational rows per scan.
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 2


async def test_independent_writers_and_restart_preserve_lost_ack():
    with TemporaryDirectory(dir=Path(__file__).parent) as directory:
        url = "sqlite+aiosqlite:///" + str(Path(directory) / "v2.db")
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app = create_app()

        async def session():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db_session] = session
        payload = measurement()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                responses = await asyncio.gather(
                    *[send(client, "/measurements", payload) for _ in range(8)]
                )
                assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
                receipt = next(r.json() for r in responses if r.status_code == 201)
        finally:
            await engine.dispose()
        # Reopen the database and construct a new app, emulating restart after ACK loss.
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        restarted = create_app()
        restarted.dependency_overrides[get_db_session] = session
        try:
            async with AsyncClient(
                transport=ASGITransport(app=restarted), base_url="http://test"
            ) as client:
                replay = await send(client, "/measurements", payload)
                assert replay.status_code == 200
                assert replay.json() == {**receipt, "duplicate": True}
            async with factory() as db:
                row = await db.scalar(select(EdgeV2Message))
                assert (
                    utc(row.received_at).isoformat()
                    == datetime.fromisoformat(receipt["received_at"]).isoformat()
                )
                assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 1
        finally:
            await engine.dispose()


def test_device_settings_rotation_and_secret_masking():
    settings = Settings(_env_file=None, EDGE_V2_DEVICES=DEVICES)
    assert TOKEN not in repr(settings)
    assert TOKEN not in repr(settings.EDGE_V2_DEVICES)
    invalid = copy.deepcopy(DEVICES)
    invalid[1]["token"] = TOKEN
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EDGE_V2_DEVICES=invalid)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, EDGE_V2_DEVICES=invalid)
    assert TOKEN not in str(error.value)
    rotated = copy.deepcopy(DEVICES)
    rotated.append({**DEVICES[0], "token": "synthetic-rotated-token"})
    assert len(Settings(_env_file=None, EDGE_V2_DEVICES=rotated).EDGE_V2_DEVICES) == 3
