"""Synthetic lock evidence, bounded diagnostic failures and cancellation cleanup."""

import asyncio
import json

import pytest
from sqlalchemy import text

from tests import pg_wait_diagnostics as diagnostic


async def test_reports_real_writer_blocker_without_sql_or_parameters(test_engine, capsys):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL lock diagnostics required")
    async with test_engine.connect() as locker, test_engine.connect() as waiter:
        locker_pid = await locker.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        query = text("SELECT username FROM auth_users WHERE username=:name FOR UPDATE")
        await locker.execute(query, {"name": "admin"})
        blocked = asyncio.create_task(waiter.execute(query, {"name": "admin"}))
        try:
            for _ in range(50):
                rows = await diagnostic.capture_waits(test_engine, waiter_pid)
                row = next(item for item in rows if item["pid"] == waiter_pid)
                if locker_pid in row["blockers"]:
                    break
                await asyncio.sleep(0.01)
            assert row["wait_event_type"] == "Lock"
            assert locker_pid in row["blockers"]
            await diagnostic.report_pending_writer(
                test_engine, {"pid": waiter_pid, "stage": "accept_message"}, delay=0
            )
            output = capsys.readouterr().out
            assert "PG_WRITER_WAIT " in output
            assert "admin" not in output and "SELECT" not in output
            report = json.loads(output.split("PG_WRITER_WAIT ")[1])
            assert report["writerPid"] == waiter_pid
        finally:
            await locker.rollback()
            await asyncio.wait_for(blocked, 2)
            await waiter.rollback()


async def test_diagnostic_connection_wait_is_bounded(capsys):
    class BlockedConnection:
        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *args):
            pass

    class Engine:
        def connect(self):
            return BlockedConnection()

    await asyncio.wait_for(
        diagnostic.report_pending_writer(Engine(), {"pid": 1, "stage": "accept_message"}, 0),
        timeout=1,
    )
    assert '"diagnosticFailure": "TimeoutError"' in capsys.readouterr().out


async def test_watchdog_cancellation_leaves_no_diagnostic(monkeypatch, capsys):
    async def unexpected(*args):
        pytest.fail("Cancelled watchdog must not access the DB")

    monkeypatch.setattr(diagnostic, "capture_waits", unexpected)
    task = asyncio.create_task(
        diagnostic.report_pending_writer(None, {"pid": 1, "stage": "accept_message"})
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert capsys.readouterr().out == ""
