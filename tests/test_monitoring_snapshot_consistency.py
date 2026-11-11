"""A real receiver commit between display reads must not mix observation generations."""

import asyncio
import copy
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.monitoring_v2 import read_snapshot
from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Measurement
from src.app.services import active_alerts_v2_service as alert_service
from src.app.services import monitoring_v2_service as service
from src.app.services.edge_v2_service import accept_message
from tests.test_height_fill_pipeline import START, frame, prepare
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_monitoring_v2 import login


@pytest.mark.parametrize(
    "path", ["control", "snapshot", "snapshot-with-alerts", "active-alerts", "sse"]
)
async def test_receiver_commit_cannot_mix_display_observation_and_filter(
    client, test_db_session, test_engine, height_context, monkeypatch, path
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Independent concurrent receiver requires PostgreSQL")
    await prepare(client, test_db_session)
    for second in range(5):
        previous, snapshot = await frame(
            client, test_db_session, height_context, second, 50, wire=False
        )
    assert snapshot.summary.filterState == "READY"
    await login(client)
    now = START + timedelta(seconds=5)
    height_context["now"] = now
    monkeypatch.setattr(alert_service, "datetime", service.datetime)
    next_body = copy.deepcopy(previous)
    next_body.update(message_id="concurrent-next-display", generated_at=now.isoformat())
    for index, sensor in enumerate(next_body["sensors"]):
        sensor["scan"].update(
            scan_id=f"concurrent-next-display-{index}",
            sequence=5,
            acquired_at=now.isoformat(),
            acquired_monotonic_ns=str(6 * 10**9),
        )
    original = AsyncSession.get
    committed = False

    async def commit_after_observation(reading, entity, ident, *args, **kwargs):
        nonlocal committed
        result = await original(reading, entity, ident, *args, **kwargs)
        if entity is EdgeV2Latest and ident[-1] == "height_fill" and not committed:
            committed = True
            async with AsyncSession(test_engine, expire_on_commit=False) as writer:
                await asyncio.wait_for(
                    accept_message(
                        writer,
                        Measurement.model_validate(next_body),
                        next_body,
                        get_settings().EDGE_V2_DEVICES[0],
                    ),
                    5,
                )
        return result

    monkeypatch.setattr(AsyncSession, "get", commit_after_observation)
    if path == "control":
        async with AsyncSession(test_engine) as reading:
            result = await service.build_snapshot(
                reading, service.monitoring_source("pit-demo"), now
            )
        basis = result.model_dump(mode="json")
        assert basis["dataState"] == "UNKNOWN"
        assert "FILTER_UNAVAILABLE" in basis["reasonCodes"]
    elif path == "sse":
        async with AsyncSession(test_engine) as reading:
            result = await read_snapshot(reading, service.monitoring_source("pit-demo"))
            assert not reading.in_transaction()
        assert result.dataState == "FRESH"
        assert result.summary.filterState == "READY"
        assert result.fillObservation.message_id == previous["message_id"]
    else:
        response = await client.get(f"/api/v2/monitoring/{path}?pit_id=pit-demo")
        assert response.status_code == 200
        body = response.json()
        basis = body["snapshot"] if path == "snapshot-with-alerts" else body
        if path == "active-alerts":
            basis = body["basis"]
            assert basis["filterState"] == "READY"
        else:
            assert basis["summary"]["filterState"] == "READY"
            assert basis["summary"]["loadPercent"] == 50
            assert basis["fillObservation"]["message_id"] == previous["message_id"]
        assert basis["dataState"] == "FRESH"
        assert "FILTER_UNAVAILABLE" not in basis["reasonCodes"]
    assert committed
    async with AsyncSession(test_engine) as observer:
        current = await observer.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_fill"))
        assert current.message_id == next_body["message_id"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_monitoring_snapshot_releases_connection_without_ending_caller_transaction(
    test_db_session, test_engine, height_context, monkeypatch, cancel
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL snapshot connection lifecycle required")
    caller = test_db_session
    await caller.execute(text("SELECT 1"))
    count = test_engine.pool.checkedout()

    async def fail(reading, source, now=None):
        assert reading is not caller
        assert await reading.scalar(text("SHOW transaction_read_only")) == "on"
        assert await reading.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("synthetic snapshot failure")

    monkeypatch.setattr(service, "build_snapshot", fail)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await service.monitoring_snapshot(caller, service.monitoring_source("pit-demo"), START)
    assert test_engine.pool.checkedout() == count
    assert caller.in_transaction()
    assert await caller.scalar(text("SELECT 1")) == 1
