"""Bounded diagnostics for synthetic PostgreSQL tests; never print SQL or parameters."""

import asyncio
import faulthandler
import gc
import json
from contextlib import contextmanager
from time import perf_counter

from sqlalchemy import text


@contextmanager
def receiver_diagnostics(writer):
    """A native traceback timer still records stalls that prevent asyncio callbacks."""
    started = perf_counter()
    gc_started = None
    pauses = []

    def collect(phase, info):
        nonlocal gc_started
        if phase == "start":
            gc_started = perf_counter()
        elif gc_started is not None:
            duration = perf_counter() - gc_started
            if duration >= 0.01:
                pauses.append({"generation": info["generation"], "seconds": duration})
            gc_started = None

    print("PG_RECEIVER_BEGIN " + json.dumps({"gcThresholds": gc.get_threshold()}), flush=True)
    gc.callbacks.append(collect)
    try:
        # Traceback frames contain no SQL parameters or Python local values.
        faulthandler.dump_traceback_later(3)
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        gc.callbacks.remove(collect)
        print(
            "PG_RECEIVER_END "
            + json.dumps(
                {"elapsedSeconds": perf_counter() - started, "writer": writer, "gcPauses": pauses}
            ),
            flush=True,
        )


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
    started = perf_counter()
    await asyncio.sleep(delay)
    print(
        "PG_WRITER_SAMPLING "
        + json.dumps({"elapsedSeconds": perf_counter() - started, "writer": writer}),
        flush=True,
    )
    try:
        pid = writer.get("pid")
        report = {"writerPid": pid, "stage": writer["stage"]}
        report["activity"] = await capture_waits(engine, pid) if pid is not None else []
        print("PG_WRITER_WAIT " + json.dumps(report), flush=True)
    except asyncio.CancelledError:
        print("PG_WRITER_WAIT " + json.dumps({"diagnosticCancelled": True}), flush=True)
        raise
    except Exception as exc:
        # A diagnostic failure must not replace the original receiver timeout.
        print("PG_WRITER_WAIT " + json.dumps({"diagnosticFailure": type(exc).__name__}), flush=True)
