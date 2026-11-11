"""Historical evidence retains atomic storage without holding the live device lock."""

import asyncio
import copy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import get_settings
from src.app.core.edge_v2_security import EdgeV2Error
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest
from src.app.schemas.edge_v2 import Measurement
from src.app.services import edge_v2_projection, edge_v2_service
from tests.test_compact_lidar_storage import START, scan_message
from tests.test_compact_lidar_storage import compact_config as compact_config
from tests.test_edge_v2 import send


def evidence(index=0):
    value = scan_message(index)
    value.update(
        delivery_kind="EVIDENCE",
        report_interval_ms=None,
        selection_policy="REFERENCED_SCANS",
        pair_state="NOT_APPLICABLE",
        assessment_ids=["synthetic-history"],
    )
    value["sensors"] = value["sensors"][:1]
    return value


async def test_evidence_skips_live_lock_but_preserves_receipts_and_immutable_conflicts(
    client, test_db_session, compact_config, monkeypatch
):
    async def unexpected(*args):
        raise AssertionError("Evidence must not acquire the live projection lock")

    monkeypatch.setattr(edge_v2_projection, "lock_projection", unexpected)
    value = evidence()
    first = await send(
        client,
        "/measurements",
        value,
        token=get_settings().EDGE_V2_DEVICES[0].token.get_secret_value(),
    )
    assert first.status_code == 201
    again = await send(
        client,
        "/measurements",
        value,
        token=get_settings().EDGE_V2_DEVICES[0].token.get_secret_value(),
    )
    assert again.status_code == 200 and again.json()["duplicate"] is True
    changed = copy.deepcopy(value)
    changed["message_id"] = "changed-evidence"
    changed["sensors"][0]["scan"]["points"][0]["distance_mm"] += 1
    conflict = await send(
        client,
        "/measurements",
        changed,
        token=get_settings().EDGE_V2_DEVICES[0].token.get_secret_value(),
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "ARTIFACT_CONFLICT"
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Message)) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 2
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Latest)) == 0
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Filter)) == 0


async def test_periodic_still_uses_live_lock(client, compact_config, monkeypatch):
    original = edge_v2_projection.lock_projection
    seen = []

    async def tracked(db, payload):
        seen.append(payload.delivery_kind)
        return await original(db, payload)

    monkeypatch.setattr(edge_v2_projection, "lock_projection", tracked)
    response = await send(
        client,
        "/measurements",
        scan_message(0),
        token=get_settings().EDGE_V2_DEVICES[0].token.get_secret_value(),
    )
    assert response.status_code == 201 and seen == ["PERIODIC"]


async def test_postgresql_periodic_can_commit_while_evidence_storage_is_paused(
    test_engine, compact_config, monkeypatch
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL writers required")
    compact_config["now"] = START + timedelta(seconds=2)
    device = get_settings().EDGE_V2_DEVICES[0]
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = edge_v2_service.store_artifacts

    async def paused(db, payload):
        if isinstance(payload, Measurement) and payload.delivery_kind == "EVIDENCE":
            entered.set()
            await release.wait()
        return await original(db, payload)

    monkeypatch.setattr(edge_v2_service, "store_artifacts", paused)

    async def write(value):
        async with factory() as db:
            return await edge_v2_service.accept_message(
                db, Measurement.model_validate(value), value, device
            )

    task = asyncio.create_task(write(evidence()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        ack = await asyncio.wait_for(write(scan_message(1)), 5)
        assert ack.accepted and not ack.duplicate and not task.done()
    finally:
        release.set()
        await task
    async with factory() as db:
        latest = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "measurement"))
        assert latest.message_id == scan_message(1)["message_id"]
        assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 2


@pytest.mark.parametrize("conflicting", [False, True])
async def test_postgresql_evidence_writers_keep_atomic_immutable_storage(
    test_engine, compact_config, conflicting
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL writers required")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    device = get_settings().EDGE_V2_DEVICES[0]
    first = evidence()
    other = copy.deepcopy(first)
    other["message_id"] = "independent-evidence"
    if conflicting:
        other["sensors"][0]["scan"]["points"][0]["distance_mm"] += 1

    async def write(value):
        async with factory() as db:
            try:
                ack = await edge_v2_service.accept_message(
                    db, Measurement.model_validate(value), value, device
                )
                return 201 if not ack.duplicate else 200
            except EdgeV2Error as exc:
                assert exc.code == "ARTIFACT_CONFLICT"
                return exc.status

    results = await asyncio.gather(write(first), write(other))
    assert sorted(results) == ([201, 409] if conflicting else [201, 201])
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == (
            1 if conflicting else 2
        )
        assert await db.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 2
        assert await db.scalar(select(func.count()).select_from(EdgeV2Latest)) == 0
