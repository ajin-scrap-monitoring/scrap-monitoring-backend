"""Read-only source safety, complete comparison and safe transfer CLI failures."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from src.app.db.base import Base
from src.app.db.transfer import TransferError, batches, copy_table, row_bytes, sqlite_url, summarize
from tests.transfer_fixtures import seed_transfer_source


async def test_readonly_source_and_bounded_copy_preserve_every_column(tmp_path):
    path = tmp_path / "source with spaces #한글.db"
    await seed_transfer_source(path)
    original = path.read_bytes()
    source = create_async_engine(sqlite_url(path))
    target = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    try:
        async with source.connect() as read, target.begin() as write:
            await read.exec_driver_sql("BEGIN")
            await write.run_sync(Base.metadata.create_all)
            for table in Base.metadata.sorted_tables:
                await copy_table(read, write, table, 1)
                assert await summarize(read, table, 1) == await summarize(write, table, 2)
            job = Base.metadata.tables["media_clip_jobs"]
            rows = [
                row for batch in [batch async for batch in batches(read, job, 1)] for row in batch
            ]
            assert len(rows) == 2
            assert row_bytes(rows[0]) != row_bytes(rows[1])
        async with source.connect() as read:
            with pytest.raises(OperationalError):
                await read.execute(text("DELETE FROM auth_users"))
        assert path.read_bytes() == original
    finally:
        await source.dispose()
        await target.dispose()


def test_missing_source_does_not_create_a_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        sqlite_url(path)
    assert not path.exists()


def test_row_comparison_is_utc_and_order_stable_and_detects_changed_values():
    first = {"time": datetime(2030, 1, 1, tzinfo=UTC), "json": {"b": 2, "a": [None, "한글"]}}
    shifted = {
        "json": {"a": [None, "한글"], "b": 2},
        "time": datetime(2030, 1, 1, 9, tzinfo=timezone(timedelta(hours=9))),
    }
    assert row_bytes(first) == row_bytes(shifted)
    assert row_bytes(first) != row_bytes({**first, "json": {"b": 3}})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"nested": float("nan")}])
def test_nonfinite_values_are_rejected_without_echo(value):
    with pytest.raises(TransferError):
        row_bytes({"payload": value})


@pytest.mark.parametrize("mode", ["check", "apply", "verify"])
async def test_cli_failure_never_echoes_credentials_or_creates_source(tmp_path, mode):
    import asyncio

    source = tmp_path / "missing.db"
    secret = "synthetic-sensitive-value"
    env = dict(os.environ, DATABASE_URL=f"postgresql+asyncpg://u:{secret}@127.0.0.1:1/check")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.app.cli.transfer",
        mode,
        "--source",
        str(source),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 1
    assert secret.encode() not in stdout + stderr
    assert b"Traceback" not in stderr
    assert not source.exists()


async def test_sql_and_json_null_remain_distinct(tmp_path):
    path = tmp_path / "nulls.db"
    await seed_transfer_source(path)
    engine = create_async_engine(sqlite_url(path))
    try:
        async with engine.connect() as read:
            result = (
                (
                    await read.execute(
                        text(
                            "SELECT id, snapshot, snapshot IS NULL AS is_sql_null "
                            "FROM media_clip_jobs ORDER BY id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert json.loads(result[0]["snapshot"]) is None
            assert result[0]["is_sql_null"] == 0
            assert result[1]["is_sql_null"] == 1
    finally:
        await engine.dispose()


async def test_read_transaction_keeps_same_snapshot_while_wal_writer_commits(tmp_path):
    path = tmp_path / "snapshot.db"
    writer = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with writer.begin() as db:
            await db.execute(text("PRAGMA journal_mode=WAL"))
            await db.execute(text("CREATE TABLE synthetic (id INTEGER PRIMARY KEY)"))
            await db.execute(text("INSERT INTO synthetic VALUES (1)"))
        reader = create_async_engine(sqlite_url(path))
        try:
            async with reader.connect() as read:
                await read.exec_driver_sql("BEGIN")
                assert await read.scalar(text("SELECT count(*) FROM synthetic")) == 1
                async with writer.begin() as db:
                    await db.execute(text("INSERT INTO synthetic VALUES (2)"))
                assert await read.scalar(text("SELECT count(*) FROM synthetic")) == 1
            async with reader.connect() as read:
                assert await read.scalar(text("SELECT count(*) FROM synthetic")) == 2
        finally:
            await reader.dispose()
    finally:
        await writer.dispose()
