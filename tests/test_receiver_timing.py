"""Synthetic timing diagnostics must preserve receipt semantics and exclude payloads."""

import asyncio
import hashlib
import json
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message
from src.app.services import edge_v2_projection, edge_v2_service, receiver_timing
from tests.test_edge_v2 import (
    TOKEN,
    assessment,
    configuration,
    measurement,
    status_report,
)
from tests.test_edge_v2 import send as send_request
from tests.test_edge_v2 import v2_credentials as v2_credentials


async def send(client, path, payload, token=TOKEN):
    return await send_request(client, "/" + path, payload, token=token)


@pytest.fixture(autouse=True)
def diagnostics(monkeypatch, caplog):
    monkeypatch.setenv("EDGE_V2_RECEIVER_TIMING_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger=receiver_timing.__name__)
    yield
    get_settings.cache_clear()


def events(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == receiver_timing.__name__
    ]


@pytest.mark.parametrize(
    "path,factory",
    [
        ("measurements", measurement),
        ("assessments", assessment),
        ("status", status_report),
        ("configurations", configuration),
    ],
)
async def test_timing_preserves_store_replay_conflict_and_privacy(client, caplog, path, factory):
    value = factory("synthetic-sensitive-id")
    first = await send(client, path, value)
    assert first.status_code == 201
    replay = await send(client, path, value)
    assert replay.status_code == 200 and replay.json() == {**first.json(), "duplicate": True}
    value["generated_at"] = "2026-09-20T01:00:01.001Z"
    assert (await send(client, path, value)).status_code == 409
    logs = events(caplog)
    assert [e["http_status"] for e in logs] == [201, 200, 409]
    assert logs[0]["message_sha256"] == hashlib.sha256(b"synthetic-sensitive-id").hexdigest()
    assert "commit" in logs[0]["phases_ms"] and "commit" not in logs[1]["phases_ms"]
    assert logs[0]["total_ms"] >= sum(logs[0]["phases_ms"].values()) - 0.01
    diagnostic_text = json.dumps(logs)
    for private in [TOKEN, "synthetic-sensitive-id", "site-demo", "scan-a-1", "position_mm"]:
        assert private not in diagnostic_text
    if path == "measurements":
        assert logs[0]["delivery_kind"] == "PERIODIC"
    if path == "assessments":
        assert logs[0]["assessment_type"] == "STATE"


async def test_projection_wait_is_separate_from_processing(client, caplog, monkeypatch):
    lock = edge_v2_projection.lock_projection

    async def slow_lock(*args, **kwargs):
        await asyncio.sleep(0.04)
        return await lock(*args, **kwargs)

    monkeypatch.setattr(edge_v2_projection, "lock_projection", slow_lock)
    assert (await send(client, "measurements", measurement())).status_code == 201
    event = events(caplog)[0]
    assert event["phases_ms"]["projection_lock"] >= 35
    assert event["total_ms"] >= event["phases_ms"]["projection_lock"]
    assert "projection" in event["phases_ms"]


async def test_failure_records_stage_without_exception_or_committed_receipt(
    client, caplog, monkeypatch, test_db_session
):
    async def fail(*args):
        raise OperationalError("private-query", {}, Exception("private-db-secret"))

    monkeypatch.setattr(edge_v2_service, "store_artifacts", fail)
    response = await send(client, "measurements", measurement())
    assert response.status_code == 503
    assert await test_db_session.scalar(select(EdgeV2Message)) is None
    logs = events(caplog)
    assert logs[0]["http_status"] == 503 and "artifacts" in logs[0]["phases_ms"]
    assert "commit" not in logs[0]["phases_ms"]
    assert "private-db-secret" not in json.dumps(logs)


async def test_disabled_and_threshold_suppress_events(client, caplog, monkeypatch):
    monkeypatch.setenv("EDGE_V2_RECEIVER_TIMING_ENABLED", "false")
    get_settings.cache_clear()
    assert (await send(client, "measurements", measurement())).status_code == 201
    assert events(caplog) == []
    monkeypatch.setenv("EDGE_V2_RECEIVER_TIMING_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_RECEIVER_TIMING_MIN_MS", "60000")
    get_settings.cache_clear()
    assert (await send(client, "measurements", measurement())).status_code == 200
    assert events(caplog) == []


async def test_unauthenticated_and_invalid_payloads_are_not_logged(client, caplog):
    assert (await send(client, "measurements", measurement(), token="invalid")).status_code == 401
    value = measurement()
    value["untrusted"] = "private-content"
    assert (await send(client, "measurements", value)).status_code == 422
    assert events(caplog) == []


async def test_logging_failure_cannot_change_committed_ack(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic logging failure")

    monkeypatch.setattr(receiver_timing.logger, "info", fail)
    assert (await send(client, "measurements", measurement())).status_code == 201
    assert (await send(client, "measurements", measurement())).json()["duplicate"] is True
