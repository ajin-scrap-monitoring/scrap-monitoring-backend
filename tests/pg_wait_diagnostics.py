"""Bounded diagnostics for synthetic PostgreSQL tests; never print SQL or parameters."""

import asyncio
import json

from sqlalchemy import text


async def capture_waits(engine, pid):
    async with asyncio.timeout(0.5):
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout = '300ms'"))
            rows = await connection.execute(
                text(
                    "SELECT pid, state, wait_event_type, wait_event, "
                    "EXTRACT(EPOCH FROM clock_timestamp()-query_start)::float "
                    "AS query_age_seconds, pg_blocking_pids(pid) AS blockers "
                    "FROM pg_stat_activity "
                    "WHERE pid=:pid OR pid=ANY(pg_blocking_pids(:pid)) "
                    "ORDER BY pid"
                ),
                {"pid": pid},
            )
            return [dict(row) for row in rows.mappings()]


async def report_pending_writer(engine, writer, delay=3):
    """Observe before the existing five-second deadline, without extending it."""
    await asyncio.sleep(delay)
    try:
        pid = writer.get("pid")
        report = {"writerPid": pid, "stage": writer["stage"]}
        report["activity"] = await capture_waits(engine, pid) if pid is not None else []
        print("PG_WRITER_WAIT " + json.dumps(report), flush=True)
    except Exception as exc:
        # A diagnostic failure must not replace the original receiver timeout.
        print("PG_WRITER_WAIT " + json.dumps({"diagnosticFailure": type(exc).__name__}), flush=True)
