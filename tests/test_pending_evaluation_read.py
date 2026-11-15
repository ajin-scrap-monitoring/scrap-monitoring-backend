"""A bounded read retry waits for actual judgments without extending their validity."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.app.services import monitoring_v2_service as service


def snapshot(state="FRESH", pending=True):
    return SimpleNamespace(
        dataState=state,
        reasonCodes=["OPERATIONS_EVALUATION_UNAVAILABLE"] if pending else [],
        operations=SimpleNamespace(effectiveState="UNKNOWN" if pending else "NORMAL"),
    )


def database():
    return SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )


@pytest.mark.parametrize("next_state", ["FRESH", "UNKNOWN"])
async def test_retry_returns_actual_new_snapshot(monkeypatch, next_state):
    pending, committed = snapshot(), snapshot(next_state, pending=False)
    results = iter([pending, committed])

    async def read(*args):
        return next(results)

    monkeypatch.setattr(service, "postgres_snapshot", read)
    assert await service.monitoring_snapshot(database(), None) is committed


async def test_retry_deadline_cancels_read_and_retains_unknown(monkeypatch):
    pending = snapshot()
    calls = 0
    closed = asyncio.Event()

    async def read(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return pending
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(service, "postgres_snapshot", read)
    result = await asyncio.wait_for(service.monitoring_snapshot(database(), None), timeout=0.5)
    assert result is pending and result.operations.effectiveState == "UNKNOWN"
    assert calls == 2 and closed.is_set()


@pytest.mark.parametrize("state,pending", [("UNKNOWN", True), ("FRESH", False)])
async def test_non_pending_data_is_not_retried(monkeypatch, state, pending):
    value = snapshot(state, pending)
    calls = 0

    async def read(*args):
        nonlocal calls
        calls += 1
        return value

    monkeypatch.setattr(service, "postgres_snapshot", read)
    assert await service.monitoring_snapshot(database(), None) is value
    assert calls == 1


async def test_retry_does_not_hide_database_timeout(monkeypatch):
    calls = 0

    async def read(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return snapshot()
        raise TimeoutError("synthetic database timeout")

    monkeypatch.setattr(service, "postgres_snapshot", read)
    with pytest.raises(TimeoutError, match="synthetic database timeout"):
        await service.monitoring_snapshot(database(), None)


async def test_retry_releases_postgresql_snapshot_before_sleep(
    test_db_session, test_engine, monkeypatch
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL read lifecycle required")
    await test_db_session.execute(text("SELECT 1"))
    baseline = test_engine.pool.checkedout()
    calls = 0
    pending, committed = snapshot(), snapshot(pending=False)

    async def build(reading, source, now=None):
        nonlocal calls
        calls += 1
        assert await reading.scalar(text("SHOW transaction_read_only")) == "on"
        assert await reading.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        return pending if calls == 1 else committed

    original_sleep = asyncio.sleep

    async def sleep(delay):
        if delay > 0:
            assert test_engine.pool.checkedout() == baseline
        await original_sleep(delay)

    monkeypatch.setattr(service, "build_snapshot", build)
    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    assert await service.monitoring_snapshot(test_db_session, None) is committed
    assert test_engine.pool.checkedout() == baseline
    assert test_db_session.in_transaction()
