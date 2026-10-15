"""Synthetic dashboard integration: ordering, missing values, isolation and SSE lifecycle."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from src.app.api.monitoring_v2 import events, read_snapshot
from src.app.core.config import Settings, get_settings
from src.app.db.session import get_db_session
from src.app.main import create_app
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.schemas.session import Session
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from src.app.services.session_service import session_service

NOW = datetime.now(UTC)
TOKEN = "synthetic-dashboard-device-token"
DEVICE = {
    "site_id": "site-demo",
    "edge_id": "edge-demo",
    "sensor_ids": ["lidar-a", "lidar-b"],
    "token": TOKEN,
}
SOURCE = {"pit_id": "pit-demo", "site_id": "site-demo", "edge_id": "edge-demo"}
BASE = "/api/v2/monitoring"


@pytest.fixture(autouse=True)
def monitoring_config(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    get_settings.cache_clear()

    class MonitoringClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    # Payload defaults use the collection-time NOW. Keep receiver timestamps and
    # snapshot freshness on that same clock so CI duration cannot expire fixtures.
    # Timeout tests advance time explicitly through build_snapshot(..., now=...).
    monkeypatch.setattr("src.app.services.edge_v2_service.datetime", MonitoringClock)
    monkeypatch.setattr("src.app.services.monitoring_v2_service.datetime", MonitoringClock)
    yield
    get_settings.cache_clear()


def envelope(kind, identifier, when=NOW):
    return {
        "schema_version": "2.0",
        "message_type": kind,
        "message_id": identifier,
        "site_id": "site-demo",
        "edge_id": "edge-demo",
        "generated_at": when.isoformat(),
        "config_revision": "cfg-demo",
    }


def assessment(identifier="a-1", ratio=0.725, when=NOW):
    return {
        **envelope("edge_assessment", identifier, when),
        "assessment_type": "STATE",
        "assessment_id": identifier,
        "revision": 1,
        "observed_from": when.isoformat(),
        "observed_to": when.isoformat(),
        "rule_version": "synthetic-rule-1",
        "decision_state": "AVAILABLE",
        "reason_codes": [],
        "fill_state": "SYNTHETIC_FILL_STATE",
        "fill_estimate": {"ratio": ratio, "method_version": "synthetic-method-1"},
        "event": None,
        "capabilities": {
            "fill": "ENABLED",
            "occlusion": "NOT_IMPLEMENTED",
            "collection": "NOT_IMPLEMENTED",
            "vision": "NOT_IMPLEMENTED",
        },
        "evidence": {"scans": [], "vision_observations": []},
    }


def status(identifier="s-1", when=NOW, sequence=1):
    return {
        **envelope("edge_status", identifier, when),
        "orchestrator_instance_id": "orch-demo",
        "sequence": sequence,
        "overall_state": "HEALTHY",
        "reason_codes": [],
        "clock": {"state": "SYNCED", "offset_ms": 0},
        "sensors": [
            {
                "sensor_id": sensor,
                "data_state": "FRESH",
                "last_scan_at": when.isoformat(),
                "reason_codes": [],
            }
            for sensor in DEVICE["sensor_ids"]
        ],
        "services": [],
        "delivery": {
            "pending_messages": 0,
            "pending_bytes": 0,
            "quarantined_messages": 0,
            "last_backend_ack_at": None,
        },
        "storage": {
            "limit_bytes": 1000,
            "used_bytes": 0,
            "lost_messages": 0,
            "loss_from": None,
            "loss_to": None,
            "last_loss_reason": None,
            "unavailable_evidence_scan_ids": [],
        },
    }


async def send(client, payload):
    route = "assessments" if payload["message_type"] == "edge_assessment" else "status"
    result = await client.post(
        "/api/edge/v2/" + route,
        json=payload,
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": payload["message_id"]},
    )
    assert result.status_code in (200, 201), result.text
    return result


async def login(client):
    response = await client.post(
        "/api/v1/session", json={"username": "operator", "password": "operator1234"}
    )
    assert response.status_code == 200
    return response.cookies["scrap_session"], Session.model_validate(response.json())


async def snap(db, now=NOW):
    return await build_snapshot(db, monitoring_source("pit-demo"), now)


@pytest.mark.parametrize("path", ["/snapshot", "/events"])
async def test_dashboard_requires_browser_session(client, path):
    response = await client.get(
        BASE + path, params={"pit_id": "pit-demo"}, headers={"Authorization": "Bearer " + TOKEN}
    )
    assert response.status_code == 401


async def test_empty_dashboard_and_unmapped_pit(client):
    await login(client)
    r = await client.get(BASE + "/snapshot", params={"pit_id": "pit-demo"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["schemaVersion"] == "2"
    assert body["dataState"] == "NO_DATA"
    assert body["summary"]["loadPercent"] is None
    assert body["assessment"] is None and body["measuredAt"] is None
    assert body["deviceStatus"]["connection_state"] == "UNKNOWN"
    assert TOKEN not in r.text
    r = await client.get(BASE + "/snapshot", params={"pit_id": "pit-other"})
    assert r.status_code == 404


@pytest.mark.parametrize("ratio,percent", [(0.0, 0.0), (0.725, 72.5), (1.0, 100.0)])
async def test_ratio_conversion_thresholds_and_no_side_effects(
    client, test_db_session, ratio, percent
):
    db = test_db_session
    db.add(
        PitConfig(
            id="pit-demo",
            name="Synthetic pit",
            warning_threshold_percent=70,
            critical_threshold_percent=90,
        )
    )
    await db.commit()
    await send(client, status())
    await send(client, assessment(ratio=ratio))
    result = await snap(db)
    assert result.dataState == "FRESH"
    assert result.summary.loadPercent == percent
    assert result.summary.warningThresholdPercent == 70
    assert result.summary.collectionThresholdPercent == 90
    assert result.assessment.fill_estimate.ratio == ratio
    assert result.assessment.fill_state == "SYNTHETIC_FILL_STATE"
    assert len(result.deviceStatus.snapshot.sensors) == 2
    for model in (ScrapMetric, AlertLog, CollectionLog, MediaClipJob):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "case,state,reason",
    [
        ("unavailable", "UNAVAILABLE", "FILL_NOT_VALIDATED"),
        ("stale", "STALE", "ASSESSMENT_STALE"),
        ("no_status", "UNKNOWN", "STATUS_NOT_RECEIVED"),
        ("future", "UNKNOWN", "ASSESSMENT_CLOCK_SKEW"),
        ("no_observation_time", "UNKNOWN", "OBSERVATION_TIME_UNAVAILABLE"),
        ("config_mismatch", "UNKNOWN", "CONFIG_REVISION_MISMATCH"),
        ("bad_device", "UNAVAILABLE", "DEVICE_MEASUREMENT_UNAVAILABLE"),
    ],
)
async def test_unknown_values_never_become_zero(client, test_db_session, case, state, reason):
    a = assessment()
    s = status()
    if case == "unavailable":
        a.update(
            fill_estimate=None,
            fill_state="UNAVAILABLE",
            decision_state="UNAVAILABLE",
            reason_codes=["FILL_NOT_VALIDATED"],
            rule_version=None,
        )
        a["capabilities"]["fill"] = "NOT_VALIDATED"
    elif case == "stale":
        a = assessment(when=NOW - timedelta(seconds=30))
    elif case == "future":
        a = assessment(when=NOW + timedelta(minutes=5))
    elif case == "no_observation_time":
        a.update(observed_from=None, observed_to=None)
    elif case == "config_mismatch":
        s["config_revision"] = "cfg-new"
    elif case == "bad_device":
        s["overall_state"] = "MEASUREMENT_UNAVAILABLE"
    if case != "no_status":
        await send(client, s)
    await send(client, a)
    result = await snap(test_db_session)
    assert result.dataState == state
    assert result.summary.loadPercent is None
    assert reason in result.reasonCodes
    assert result.assessment is not None


async def test_latest_state_excludes_events_and_late_messages(client, test_db_session):
    await send(client, status())
    current = assessment("current", 0.8)
    await send(client, current)
    initial = await snap(test_db_session)
    await send(client, assessment("older", 0.1, NOW - timedelta(seconds=5)))
    event = assessment("event", 0.9, NOW + timedelta(seconds=1))
    event.update(
        assessment_type="EVENT", revision=5, event={"kind": "COLLECTION", "stage": "RETRACTED"}
    )
    await send(client, event)
    await send(client, current)
    updated = await snap(test_db_session)
    assert updated.assessment.assessment_id == "current"
    assert updated.summary.loadPercent == 80
    assert updated.snapshotId == initial.snapshotId


async def test_device_and_site_isolation(client, test_db_session, monkeypatch):
    other = {**DEVICE, "edge_id": "edge-other", "token": "synthetic-other-token"}
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE, other]))
    get_settings.cache_clear()
    payload = assessment("other-assessment")
    payload["edge_id"] = "edge-other"
    result = await client.post(
        "/api/edge/v2/assessments",
        json=payload,
        headers={
            "Authorization": "Bearer synthetic-other-token",
            "Idempotency-Key": payload["message_id"],
        },
    )
    assert result.status_code == 201
    result = await snap(test_db_session)
    assert result.assessment is None


async def test_timeout_and_replay_do_not_refresh_current_value(client, test_db_session):
    a, s = assessment(), status()
    await send(client, a)
    await send(client, s)
    db = test_db_session
    row = await db.scalar(select(EdgeV2Message).where(EdgeV2Message.message_id == "s-1"))
    row.received_at = NOW - timedelta(seconds=30)
    await db.commit()
    replay = await send(client, s)
    assert replay.json()["duplicate"] is True
    result = await snap(db)
    assert result.dataState == "UNKNOWN"
    assert result.summary.loadPercent is None
    assert result.deviceStatus.connection_state == "UNKNOWN"
    assert result.assessment.fill_estimate.ratio == 0.725


async def test_old_observation_in_recent_message_is_stale(client, test_db_session):
    await send(client, status())
    a = assessment()
    a["observed_from"] = a["observed_to"] = (NOW - timedelta(minutes=1)).isoformat()
    await send(client, a)
    assert (await snap(test_db_session)).dataState == "STALE"


async def test_snapshot_identity_ignores_server_clock_and_read_releases_transaction(
    client, test_db_session
):
    await send(client, status())
    await send(client, assessment())
    first = await snap(test_db_session)
    second = await snap(test_db_session, NOW + timedelta(seconds=1))
    assert first.snapshotId == second.snapshotId
    await read_snapshot(test_db_session, monitoring_source("pit-demo"))
    assert not test_db_session.in_transaction()


class ConnectedRequest:
    async def is_disconnected(self):
        return False


async def test_sse_initial_update_keepalive_logout_and_cleanup(
    client, test_db_session, monkeypatch
):
    import src.app.api.monitoring_v2 as route

    real_sleep = asyncio.sleep

    async def quick_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(route.asyncio, "sleep", quick_sleep)
    session_id, session = await login(client)
    response = await events(
        ConnectedRequest(), session, monitoring_source("pit-demo"), test_db_session, session_id
    )
    assert response.headers["x-accel-buffering"] == "no"
    generator = response.body_iterator
    initial = await anext(generator)
    assert initial.startswith("event: monitoring.snapshot\n")
    assert '"loadPercent":null' in initial
    assert not test_db_session.in_transaction()
    await send(client, status())
    await send(client, assessment())
    updated = await anext(generator)
    assert '"loadPercent":72.5' in updated
    assert await anext(generator) == ": keepalive\n\n"
    await session_service.delete_session(test_db_session, session_id)
    assert await anext(generator) == "event: session.expired\ndata: {}\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(generator)
    assert not test_db_session.in_transaction()


async def test_sse_timeout_transition_without_new_messages(client, test_db_session, monkeypatch):
    import src.app.api.monitoring_v2 as route

    await send(client, status())
    await send(client, assessment())
    session_id, session = await login(client)
    response = await events(
        ConnectedRequest(), session, monitoring_source("pit-demo"), test_db_session, session_id
    )
    generator = response.body_iterator
    assert '"dataState":"FRESH"' in await anext(generator)

    async def after_timeout(db, source):
        try:
            return await build_snapshot(db, source, NOW + timedelta(minutes=1))
        finally:
            await db.rollback()

    async def quick_sleep(seconds):
        return None

    monkeypatch.setattr(route.asyncio, "sleep", quick_sleep)
    monkeypatch.setattr(route, "read_snapshot", after_timeout)
    updated = await anext(generator)
    assert '"dataState":"STALE"' in updated
    assert '"connection_state":"UNKNOWN"' in updated
    assert '"loadPercent":null' in updated
    await generator.aclose()
    assert not test_db_session.in_transaction()


@pytest.mark.parametrize(
    "bad",
    [
        [{**SOURCE, "edge_id": "missing"}],
        [SOURCE, SOURCE],
        [SOURCE, {**SOURCE, "pit_id": "pit-other"}],
    ],
)
def test_invalid_assignments_fail_fast(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EDGE_V2_DEVICES=[DEVICE], EDGE_V2_MONITORING_SOURCES=bad)


def test_token_rotation_preserves_assignment():
    devices = [DEVICE, {**DEVICE, "token": "synthetic-rotated-token"}]
    settings = Settings(
        _env_file=None, EDGE_V2_DEVICES=devices, EDGE_V2_MONITORING_SOURCES=[SOURCE]
    )
    assert settings.EDGE_V2_MONITORING_SOURCES[0].pit_id == "pit-demo"


async def test_dashboard_reads_committed_data_from_independent_session(client, test_engine):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await send(client, status())
    await send(client, assessment())
    app = create_app()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def fresh_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db_session] = fresh_db
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as reader:
        await login(reader)
        r = await reader.get(BASE + "/snapshot", params={"pit_id": "pit-demo"})
    assert r.status_code == 200
    assert r.json()["summary"]["loadPercent"] == 72.5


async def test_v1_snapshot_keeps_original_contract(client):
    await send(client, status())
    await send(client, assessment())
    r = await client.get("/api/v1/monitoring/snapshot", params={"pit_id": "pit-demo"})
    assert r.status_code == 200
    assert r.json()["schemaVersion"] == "1"
    assert r.json()["systemStatus"] == "no_data"
    assert "assessment" not in r.json()
