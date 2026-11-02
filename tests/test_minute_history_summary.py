"""Derived history summaries preserve original results, partial ranges and live writes."""

import asyncio
import json
import os
import sys
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_load_history_v2 import history_config as history_config
from test_long_load_history import START, minute
from test_monitoring_v2 import assessment, send, status

from src.app.core.config import get_settings
from src.app.db.migration import upgrade_schema
from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.services.compact_history_stream import legacy_stream_groups
from src.app.services.compact_history_summary import build_minute_summary, summary_stream_groups
from src.app.services.history_summary_backfill import backfill_batch, missing_summaries
from src.app.services.load_history_v2_service import _compact_result
from src.app.services.monitoring_v2_service import monitoring_source


def summarized(row):
    row["history_summary"] = build_minute_summary(
        row["bucket_start"], row["generated_at"], row["payload"]
    )
    return row


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "missing",
        "mixed",
        "late",
        "partial",
        "zero",
        "invalid",
        "misplaced",
        "stale",
        "bad_version",
        "bad_shape",
    ],
)
async def test_summary_and_original_readers_have_identical_results(test_db_session, mode):
    db = test_db_session
    rows = [minute(START + timedelta(minutes=i)) for i in range(4)]
    for index, row in enumerate(rows):
        payload = row["payload"]
        if mode == "mixed":
            payload["methods"] = dict.fromkeys(payload["values"], f"method-{index}")
        if mode in ("zero", "invalid"):
            payload["values"] = {
                k: 0.0 if mode == "zero" and value is not None else None
                for k, value in payload["values"].items()
            }
        if mode == "late":
            payload["last"] = payload["metadata"][min(payload["values"], key=int)]
        if mode == "misplaced":
            key = min(payload["values"], key=int)
            payload["values"][str(int(key) - 60)] = payload["values"].pop(key)
        summarized(row)
        if mode == "missing":
            row["history_summary"] = None
        if mode == "stale":
            row["history_summary"]["generatedAt"] = START.isoformat()
        if mode == "bad_version":
            row["history_summary"]["version"] = 999
        if mode == "bad_shape":
            row["history_summary"]["group"] = {}
    await db.execute(insert(EdgeV2Minute), rows)
    await db.commit()
    start = START + timedelta(seconds=2.3) if mode == "partial" else START
    end = (
        START + timedelta(minutes=3, seconds=20.1)
        if mode == "partial"
        else START + timedelta(minutes=4)
    )
    source = monitoring_source("pit-demo")
    expected = _compact_result(await legacy_stream_groups(db, source, start, end, 3600), start, end)
    actual = _compact_result(await summary_stream_groups(db, source, start, end, 3600), start, end)
    assert actual == expected


async def test_cached_complete_minutes_do_not_select_original_payload(test_db_session, test_engine):
    db = test_db_session
    await db.execute(
        insert(EdgeV2Minute), [summarized(minute(START + timedelta(minutes=i))) for i in range(5)]
    )
    await db.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        groups = await summary_stream_groups(
            db, monitoring_source("pit-demo"), START, START + timedelta(minutes=5), 3600
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert len(groups) == 1
    assert len(statements) == 1
    assert "history_summary" in statements[0] and "edge_v2_minutes.payload" not in statements[0]


async def test_partial_boundaries_fetch_only_their_originals_and_preserve_microseconds(
    test_db_session, test_engine
):
    db = test_db_session
    await db.execute(
        insert(EdgeV2Minute), [summarized(minute(START + timedelta(minutes=i))) for i in range(5)]
    )
    await db.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "edge_v2_minutes.payload" in statement:
            statements.append(parameters)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        start, end = START + timedelta(seconds=2.3), START + timedelta(minutes=4, seconds=20.1)
        groups = await summary_stream_groups(db, monitoring_source("pit-demo"), start, end, 3600)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 1 and len(statements[0]) == 4  # source IDs plus two minute keys
    actual = _compact_result(groups, start, end)
    expected = _compact_result(
        await legacy_stream_groups(db, monitoring_source("pit-demo"), start, end, 3600), start, end
    )
    assert actual == expected


async def test_backfill_is_bounded_repeatable_and_preserves_every_original_column(test_db_session):
    db = test_db_session
    await db.execute(insert(EdgeV2Minute), [minute(START + timedelta(minutes=i)) for i in range(5)])
    await db.commit()

    async def original_rows():
        return [
            (
                r.site_id,
                r.edge_id,
                r.bucket_start,
                r.generated_at,
                r.created_at,
                deepcopy(r.payload),
            )
            for r in (
                await db.scalars(select(EdgeV2Minute).order_by(EdgeV2Minute.bucket_start))
            ).all()
        ]

    before = await original_rows()
    assert await missing_summaries(db) == 5
    count, cursor = await backfill_batch(db, 2)
    assert count == 2 and await missing_summaries(db) == 3
    count, cursor = await backfill_batch(db, 2, cursor)
    assert count == 2
    count, cursor = await backfill_batch(db, 2, cursor)
    assert count == 1
    assert (await backfill_batch(db, 2, cursor))[0] == 0
    assert (await backfill_batch(db, 2))[0] == 0
    assert await missing_summaries(db) == 0
    assert await original_rows() == before


async def test_summary_reads_keep_device_scope_and_bound_missing_pages(
    test_db_session, monkeypatch
):
    import src.app.services.compact_history_summary as service

    db = test_db_session
    rows = [minute(START + timedelta(minutes=i)) for i in range(5)]
    other = summarized(minute(START, method="other-method"))
    other["edge_id"] = "other-edge"
    await db.execute(insert(EdgeV2Minute), [*rows, other])
    await db.commit()
    monkeypatch.setattr(service, "STREAM_PAGE_SIZE", 2)
    actual = await summary_stream_groups(
        db, monitoring_source("pit-demo"), START, START + timedelta(minutes=5), 3600
    )
    group = next(iter(actual.values()))
    assert group["count"] == 15 and group["methods"] == {"synthetic-v1"}


async def test_live_projection_replaces_summary_with_same_second_and_late_observations(
    client, test_db_session, monkeypatch
):
    from test_monitoring_v2 import NOW

    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    await send(client, status())
    for identifier, when, ratio in [
        ("summary-initial", NOW, 0.2),
        ("summary-replacement", NOW, 0.8),
        ("summary-next", NOW + timedelta(seconds=1), 0.5),
        ("summary-late", NOW - timedelta(seconds=1), 0.1),
    ]:
        await send(client, assessment(identifier, ratio, when))
    rows = (await test_db_session.scalars(select(EdgeV2Minute))).all()
    assert rows and all(r.history_summary is not None for r in rows)
    for row in rows:
        assert row.history_summary == build_minute_summary(
            row.bucket_start, row.generated_at, row.payload
        )
    start = NOW.replace(second=0, microsecond=0) - timedelta(minutes=1)
    end = start + timedelta(minutes=3)
    source = monitoring_source("pit-demo")
    assert await summary_stream_groups(
        test_db_session, source, start, end, 3600
    ) == await legacy_stream_groups(test_db_session, source, start, end, 3600)


async def test_postgresql_backfill_skips_live_locked_row_and_can_resume(
    test_db_session, test_engine, request
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("PostgreSQL row-lock isolation")
    db = test_db_session
    await db.execute(insert(EdgeV2Minute), [minute(START + timedelta(minutes=i)) for i in range(2)])
    await db.commit()
    async with async_sessionmaker(test_engine)() as writer:
        await writer.scalar(
            select(EdgeV2Minute).where(EdgeV2Minute.bucket_start == START).with_for_update()
        )
        assert (await backfill_batch(db, 2))[0] == 1
        assert await missing_summaries(db) == 1
        await writer.rollback()
    assert (await backfill_batch(db, 2))[0] == 1
    assert await missing_summaries(db) == 0


async def test_mixed_within_minute_and_invalid_simulation_flag_preserve_original_policy(
    test_db_session,
):
    db = test_db_session
    row = minute(START)
    payload = row["payload"]
    stamps = list(payload["values"])
    payload["methods"][stamps[0]] = "other-method"
    payload["sources"][stamps[0]] = "HEIGHT_SECTIONS"
    payload["methods"][stamps[2]] = "SIMULATION_SENSOR_ANGLE_V1:synthetic"
    await db.execute(insert(EdgeV2Minute), [summarized(row)])
    await db.commit()
    source, end = monitoring_source("pit-demo"), START + timedelta(minutes=1)
    expected = _compact_result(await legacy_stream_groups(db, source, START, end, 60), START, end)
    actual = _compact_result(await summary_stream_groups(db, source, START, end, 60), START, end)
    assert actual == expected
    point = next(iter(actual.values()))[1]
    assert point["inputSource"] == "MIXED" and point["valuePercent"] is None
    assert point["validSamples"] == 0 and point["invalidSamples"] == 4 and point["simulated"]


async def cli(url, *args):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.app.cli.history_summaries",
        *args,
        env={**os.environ, "DATABASE_URL": url, "APP_ENV": "test"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    return process.returncode, stdout.decode(), stderr.decode()


async def test_cli_status_backfill_batch_limit_and_repeat(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'summary-cli.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade_schema)
            await connection.execute(
                insert(EdgeV2Minute), [minute(START + timedelta(minutes=i)) for i in range(2)]
            )
        code, stdout, stderr = await cli(url, "status")
        assert code == 0 and json.loads(stdout)["remaining"] == 2, stderr
        code, stdout, stderr = await cli(
            url, "backfill", "--batch-size", "1", "--pause-seconds", "0"
        )
        assert code == 0, stderr
        assert json.loads(stdout.splitlines()[-1]) == {
            "updated": 1,
            "remaining": 1,
            "complete": False,
        }
        code, stdout, stderr = await cli(url, "backfill", "--pause-seconds", "0")
        assert code == 0 and json.loads(stdout.splitlines()[-1])["complete"], stderr
        code, stdout, stderr = await cli(url, "backfill", "--pause-seconds", "0")
        assert code == 0 and json.loads(stdout.splitlines()[-1])["updated"] == 0, stderr
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "args",
    [
        ("--batch-size", "0"),
        ("--max-batches", "0"),
        ("--pause-seconds", "nan"),
        ("--pause-seconds", "inf"),
    ],
)
async def test_cli_rejects_invalid_limits_before_database_access(args):
    code, stdout, stderr = await cli(
        "sqlite+aiosqlite:////unavailable/no-database.db", "backfill", *args
    )
    assert code == 2 and "error:" in stderr and stdout == ""
