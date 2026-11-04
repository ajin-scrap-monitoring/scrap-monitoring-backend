"""Real statistics refresh preserves receipts, enforces privileges, and bounds lock waits."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.app.models.edge_v2 import EdgeV2Message
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.history_statistics import StatisticsError, assess, maintain_statistics

NOW = datetime(2026, 1, 5, tzinfo=UTC)
SOURCE = MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")


@pytest.mark.parametrize(
    "latest,sampled,reason,needed",
    [
        (None, None, "NO_DATA", False),
        (NOW, None, "STATISTICS_BEHIND", True),
        (NOW, NOW - timedelta(days=2), "STATISTICS_BEHIND", True),
        (NOW, NOW - timedelta(days=1), "WITHIN_LAG_THRESHOLD", False),
        (NOW, NOW, "WITHIN_LAG_THRESHOLD", False),
        (NOW, NOW + timedelta(minutes=1), "FUTURE_STATISTICS", False),
        (NOW + timedelta(minutes=1), NOW, "FUTURE_DATA", False),
    ],
)
def test_lag_is_a_review_threshold_not_a_query_sla(latest, sampled, reason, needed):
    result = assess(latest, sampled, 86400, NOW)
    assert result["reason"] == reason and result["refreshNeeded"] is needed


@pytest.fixture
async def stale_statistics(test_engine, request):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Real PostgreSQL 17 maintenance")
    start = datetime.now(UTC) - timedelta(days=3)

    def row(index, stamp):
        return {
            "site_id": SOURCE.site_id,
            "edge_id": SOURCE.edge_id,
            "message_id": f"stats-{index}",
            "message_type": "edge_assessment",
            "config_revision": "synthetic-config",
            "generated_at": stamp,
            "received_at": stamp,
            "body_hash": "0" * 64,
            "payload": {},
            "compact": True,
        }

    async with test_engine.begin() as conn:
        await conn.execute(
            insert(EdgeV2Message), [row(i, start + timedelta(minutes=i)) for i in range(10)]
        )
        await conn.execute(text("ANALYZE edge_v2_messages"))
        await conn.execute(insert(EdgeV2Message), row(10, start + timedelta(days=2)))
    return test_engine


async def receipts(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(select(EdgeV2Message.__table__).order_by(EdgeV2Message.id))
        ).all()


async def test_real_check_refresh_skip_and_original_receipts_preserved(stale_statistics):
    engine = stale_statistics
    original = await receipts(engine)
    checked = await maintain_statistics(engine, [SOURCE], "check")
    assert checked["action"] == "CHECKED" and checked["before"]["refreshNeeded"]
    assert await receipts(engine) == original
    refreshed = await maintain_statistics(engine, [SOURCE], "refresh")
    assert refreshed["action"] == "ANALYZED"
    assert not refreshed["after"]["refreshNeeded"]
    assert await receipts(engine) == original
    skipped = await maintain_statistics(engine, [SOURCE], "refresh")
    assert skipped["action"] == "SKIPPED"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SHOW statement_timeout")) == "0"
        assert await conn.scalar(text("SHOW lock_timeout")) == "0"
        assert await conn.scalar(text("SHOW transaction_read_only")) == "off"


async def test_refresh_rejects_reader_without_granting_privileges(stale_statistics):
    engine = stale_statistics
    role = "statistics_reader_" + uuid4().hex
    async with engine.begin() as conn:
        schema = await conn.scalar(text("SELECT current_schema()"))
        await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        await conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'))
    reader = create_async_engine(
        engine.url, connect_args={"server_settings": {"role": role, "search_path": schema}}
    )
    try:
        result = await maintain_statistics(reader, [SOURCE], "check")
        assert result["before"]["canAnalyze"] is False
        with pytest.raises(StatisticsError, match="STATISTICS_PERMISSION_REQUIRED"):
            await maintain_statistics(reader, [SOURCE], "refresh")
        async with reader.connect() as conn:
            assert not await conn.scalar(
                text("SELECT has_table_privilege('edge_v2_messages', 'MAINTAIN')")
            )
    finally:
        await reader.dispose()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


async def test_refresh_lock_timeout_leaves_original_data(stale_statistics):
    engine = stale_statistics
    original = await receipts(engine)
    async with engine.begin() as writer:
        await writer.execute(text("LOCK TABLE edge_v2_messages IN SHARE UPDATE EXCLUSIVE MODE"))
        with pytest.raises(StatisticsError, match="STATISTICS_LOCK_TIMEOUT"):
            await maintain_statistics(engine, [SOURCE], "refresh", lock_seconds=1)
    assert await receipts(engine) == original


@pytest.mark.parametrize("analyzed", [False, True])
async def test_missing_and_single_valued_statistics(test_engine, request, analyzed):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Real PostgreSQL 17 statistics representation")
    when = datetime.now(UTC) - timedelta(minutes=5)
    async with test_engine.begin() as conn:
        await conn.execute(
            insert(EdgeV2Message),
            {
                "site_id": SOURCE.site_id,
                "edge_id": SOURCE.edge_id,
                "message_id": "one-value",
                "message_type": "edge_assessment",
                "config_revision": "synthetic-config",
                "generated_at": when,
                "received_at": when,
                "body_hash": "0" * 64,
                "payload": {},
                "compact": True,
            },
        )
        if analyzed:
            await conn.execute(
                insert(EdgeV2Message),
                {
                    "site_id": SOURCE.site_id,
                    "edge_id": SOURCE.edge_id,
                    "message_id": "same-time",
                    "message_type": "edge_assessment",
                    "config_revision": "synthetic-config",
                    "generated_at": when,
                    "received_at": when,
                    "body_hash": "0" * 64,
                    "payload": {},
                    "compact": True,
                },
            )
            await conn.execute(text("ANALYZE edge_v2_messages"))
    result = await maintain_statistics(test_engine, [SOURCE], "check")
    assert result["before"]["refreshNeeded"] is not analyzed
    if analyzed:
        assert datetime.fromisoformat(result["before"]["sampleLatestAt"]) == when
        # A lone unique value can have statistics but neither histogram nor common values.
        async with test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM edge_v2_messages WHERE message_id='same-time'"))
            await conn.execute(text("ANALYZE edge_v2_messages"))
        single = await maintain_statistics(test_engine, [SOURCE], "refresh")
        assert single["action"] == "SKIPPED"
        assert single["before"]["reason"] == "STATISTICS_RANGE_UNAVAILABLE"


async def test_empty_receipts_skip_refresh(test_engine, request):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Real PostgreSQL 17 maintenance")
    result = await maintain_statistics(test_engine, [SOURCE], "refresh")
    assert result["action"] == "SKIPPED" and result["before"]["reason"] == "NO_DATA"


async def test_statement_timeout_is_bounded_and_resets(stale_statistics):
    engine = stale_statistics

    def slow_version(conn, cursor, statement, parameters, context, executemany):
        if statement == "SHOW server_version_num":
            return "SELECT pg_sleep(2)", ()
        return statement, parameters

    event.listen(engine.sync_engine, "before_cursor_execute", slow_version, retval=True)
    try:
        with pytest.raises(StatisticsError, match="STATISTICS_STATEMENT_TIMEOUT"):
            await maintain_statistics(engine, [SOURCE], "check", statement_seconds=1)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", slow_version)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SHOW statement_timeout")) == "0"


async def test_cancellation_releases_waiting_refresh_connection(stale_statistics):
    engine = stale_statistics
    waiting = asyncio.Event()

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("ANALYZE ("):
            waiting.set()

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with engine.begin() as writer:
            await writer.execute(text("LOCK TABLE edge_v2_messages IN SHARE UPDATE EXCLUSIVE MODE"))
            task = asyncio.create_task(maintain_statistics(engine, [SOURCE], "refresh"))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert engine.pool.checkedout() == 0


def test_cli_sqlite_is_rejected_without_creating_database(tmp_path):
    path = tmp_path / "untouched.db"
    result = subprocess.run(
        [sys.executable, "-m", "src.app.cli.history_statistics", "check"],
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1 and not path.exists()
    body = json.loads(result.stdout)
    assert body["errorCode"] == "POSTGRESQL_17_REQUIRED"
    assert "elapsedSeconds" in body and str(path) not in result.stdout


def test_cli_does_not_log_connection_or_exception_details(monkeypatch, capsys):
    from src.app.cli import history_statistics as cli

    async def fail(*args):
        raise RuntimeError("postgresql://private-user:private-password@private-host/private-db")

    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr(sys, "argv", ["history_statistics", "check"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert json.loads(output)["errorCode"] == "STATISTICS_OPERATION_FAILED"
    assert "private" not in output
