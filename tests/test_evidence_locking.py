"""EVIDENCE persists immutable inputs without locking the unrelated live projection."""

import asyncio
import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.main import create_app
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest
from src.app.services import edge_v2_projection
from tests.test_edge_v2 import DEVICES, assessment, measurement, send, status_report


@pytest.fixture(autouse=True)
def compact_device(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps(DEVICES))
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def evidence(identifier="evidence-1"):
    payload = measurement(identifier)
    payload.update(
        delivery_kind="EVIDENCE",
        report_interval_ms=None,
        selection_policy="REFERENCED_SCANS",
        pair_state="NOT_APPLICABLE",
        assessment_ids=["assessment-1"],
    )
    payload["sensors"] = payload["sensors"][:1]
    return payload


async def test_evidence_skips_projection_lock_and_preserves_immutable_conflicts(
    client, test_db_session, monkeypatch
):
    async def unexpected_lock(*_args):
        raise AssertionError("historical evidence must not lock the live projection")

    monkeypatch.setattr(edge_v2_projection, "lock_projection", unexpected_lock)
    payload = evidence()
    response = await send(client, "/measurements", payload)
    assert response.status_code == 201
    replay = await send(client, "/measurements", payload)
    assert replay.status_code == 200
    assert replay.json()["received_at"] == response.json()["received_at"]
    changed = copy.deepcopy(payload)
    changed["message_id"] = "conflicting-evidence"
    changed["sensors"][0]["scan"]["points"][0]["distance_mm"] += 1
    conflict = await send(client, "/measurements", changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "ARTIFACT_CONFLICT"
    for model, count in [
        (EdgeV2Message, 1),
        (EdgeV2Artifact, 2),
        (EdgeV2Filter, 0),
        (EdgeV2Latest, 0),
    ]:
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == count


@pytest.mark.parametrize("kind", ["PERIODIC", "STATE", "STATUS"])
async def test_live_updates_keep_their_projection_lock(client, monkeypatch, kind):
    original = edge_v2_projection.lock_projection
    calls = []

    async def tracked(db, payload):
        calls.append(payload.message_id)
        return await original(db, payload)

    monkeypatch.setattr(edge_v2_projection, "lock_projection", tracked)
    route, payload = {
        "PERIODIC": ("/measurements", measurement()),
        "STATE": ("/assessments", assessment()),
        "STATUS": ("/status", status_report()),
    }[kind]
    assert (await send(client, route, payload)).status_code == 201
    assert calls == [payload["message_id"]]


@pytest.mark.parametrize("mode", ["distinct", "same-message", "conflict"])
async def test_postgresql_parallel_evidence_preserves_receipts_and_artifacts(test_engine, mode):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("requires the dedicated PostgreSQL test fixture")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    app = create_app()

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db_session] = session
    payloads = [evidence("same" if mode == "same-message" else f"parallel-{i}") for i in range(4)]
    if mode == "conflict":
        for i, payload in enumerate(payloads):
            payload["sensors"][0]["scan"]["points"][0]["distance_mm"] += i % 2
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        responses = await asyncio.gather(*(send(client, "/measurements", p) for p in payloads))
    expected = {
        "distinct": [201] * 4,
        "same-message": [200] * 3 + [201],
        "conflict": [201] * 2 + [409] * 2,
    }[mode]
    assert sorted(r.status_code for r in responses) == sorted(expected)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 2
        assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == expected.count(
            201
        )
        assert await db.scalar(select(func.count()).select_from(EdgeV2Filter)) == 0
