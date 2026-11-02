"""Real connection reuse, a bounded concurrent ceiling, and unchanged SQLite sessions."""

import asyncio
import os

import pytest
from sqlalchemy import event, text

from src.app.core.config import get_settings
from src.app.db import session as database


@pytest.fixture
async def pooled_engine(test_engine, request, monkeypatch):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Real PostgreSQL pool behavior")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_sessionmaker", None)
    monkeypatch.setattr(get_settings(), "DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
    engine = database.get_engine()
    try:
        yield engine
    finally:
        await database.close_db()


async def test_repeated_eight_reader_bursts_reuse_the_same_connections(pooled_engine):
    engine = pooled_engine
    counts = {"connect": 0, "close": 0}

    @event.listens_for(engine.sync_engine, "connect")
    def connected(*args):
        counts["connect"] += 1

    @event.listens_for(engine.sync_engine, "close")
    def closed(*args):
        counts["close"] += 1

    for _ in range(4):
        ready = asyncio.Queue()
        release = asyncio.Event()

        async def read(ready, release):
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
                await ready.put(None)
                await release.wait()

        readers = [asyncio.create_task(read(ready, release)) for _ in range(8)]
        try:
            for _ in readers:
                await asyncio.wait_for(ready.get(), timeout=10)
        finally:
            release.set()
            await asyncio.gather(*readers)
        assert counts == {"connect": 8, "close": 0}
    assert engine.pool.checkedout() == 0


async def test_sixteenth_reader_waits_until_one_of_fifteen_returns(pooled_engine):
    engine = pooled_engine
    connected = []

    @event.listens_for(engine.sync_engine, "connect")
    def capture(connection, record):
        connected.append(record)

    connections = []
    waiting = None
    try:
        for _ in range(15):
            connections.append(await engine.connect())
        assert len(connected) == 15
        started = asyncio.Event()

        async def next_connection():
            started.set()
            return await engine.connect()

        waiting = asyncio.create_task(next_connection())
        await started.wait()
        await asyncio.sleep(0)
        assert not waiting.done()
        await connections.pop().close()
        returned = await asyncio.wait_for(waiting, timeout=5)
        connections.append(returned)
        assert await returned.scalar(text("SELECT 1")) == 1
        assert len(connected) == 15
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        elif waiting is not None and not waiting.cancelled() and waiting.exception() is None:
            acquired = waiting.result()
            if acquired not in connections:
                await acquired.close()
        for connection in connections:
            await connection.close()
    assert engine.pool.checkedout() == 0


async def test_sqlite_factory_still_reuses_engine_and_closes_cleanly(monkeypatch):
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_sessionmaker", None)
    monkeypatch.setattr(get_settings(), "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    try:
        engine = database.get_engine()
        assert database.get_engine() is engine
        factory = database.get_sessionmaker()
        assert database.get_sessionmaker() is factory
        async with factory() as db:
            assert await db.scalar(text("SELECT 1")) == 1
    finally:
        await database.close_db()
    assert database._engine is None and database._sessionmaker is None
