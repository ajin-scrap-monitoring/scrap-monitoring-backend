"""Buffered history must yield to other requests and release cursors on cancellation."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert
from test_long_load_history import START, minute

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services import compact_history_stream as original
from src.app.services import compact_history_summary as summary
from src.app.services.load_history_v2_service import _compact_result


class BufferedCursor:
    """Model a driver returning every page without waiting for network I/O."""

    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    async def partitions(self, size):
        for offset in range(0, len(self.rows), size):
            yield self.rows[offset : offset + size]

    async def close(self):
        self.closed = True


class BufferedDatabase:
    def __init__(self, cursor):
        self.cursor = cursor

    async def stream(self, statement, *, execution_options):
        return self.cursor


def reader_setup(monkeypatch, mode):
    rows = []
    for index in range(original.STREAM_PAGE_SIZE * 3):
        row = minute(START + timedelta(minutes=index))
        row["history_summary"] = summary.build_minute_summary(
            row["bucket_start"], row["generated_at"], row["payload"]
        )
        rows.append(
            (row["bucket_start"], row["generated_at"], row["history_summary"])
            if mode == "summary"
            else SimpleNamespace(**row)
        )
    cursor = BufferedCursor(rows)
    counts = []
    module = summary if mode == "summary" else original
    name = "merge_summary" if mode == "summary" else "accumulate_minute"
    calculate = getattr(module, name)

    def record(*args):
        counts.append(len(counts) + 1)
        return calculate(*args)

    monkeypatch.setattr(module, name, record)
    reader = summary.summary_stream_groups if mode == "summary" else original.legacy_stream_groups
    source = MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")
    return (
        cursor,
        counts,
        reader(BufferedDatabase(cursor), source, START, START + timedelta(minutes=len(rows)), 3600),
    )


@pytest.mark.parametrize("mode", ["summary", "original"])
async def test_buffered_history_allows_another_request_before_finishing(monkeypatch, mode):
    cursor, counts, reading = reader_setup(monkeypatch, mode)
    task = asyncio.create_task(reading)
    try:
        await asyncio.sleep(0)
        # The driver has not awaited any I/O. The calculation itself must yield.
        assert 0 < len(counts) <= original.STREAM_PAGE_SIZE
        result = await task
        assert sum(group["count"] for group in result.values()) == len(cursor.rows) * 3
        assert len(counts) == len(cursor.rows)
        assert cursor.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["summary", "original"])
async def test_cancellation_interrupts_buffered_history_and_closes_cursor(monkeypatch, mode):
    cursor, counts, reading = reader_setup(monkeypatch, mode)
    task = asyncio.create_task(reading)
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert 0 < len(counts) < len(cursor.rows)
        assert cursor.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_multiple_real_pages_preserve_cached_original_and_partial_results(
    test_db_session, monkeypatch
):
    rows = []
    for index in range(original.STREAM_PAGE_SIZE * 2 + 3):
        row = minute(START + timedelta(minutes=index))
        row["history_summary"] = (
            summary.build_minute_summary(row["bucket_start"], row["generated_at"], row["payload"])
            if index % 17
            else None
        )
        rows.append(row)
    await test_db_session.execute(insert(EdgeV2Minute), rows)
    await test_db_session.commit()
    source = MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")
    start = START + timedelta(seconds=2.3)
    end = START + timedelta(minutes=len(rows) - 1, seconds=20.1)
    expected = await original.legacy_stream_groups(test_db_session, source, start, end, 900)
    allocations = []
    make_group = summary._group

    def allocate_group():
        group = make_group()
        allocations.append(group)
        return group

    monkeypatch.setattr(summary, "_group", allocate_group)
    actual = await summary.summary_stream_groups(test_db_session, source, start, end, 900)
    assert _compact_result(actual, start, end) == _compact_result(expected, start, end)
    # Cached minutes share one accumulator per output bucket, including across pages.
    assert 0 < len(allocations) <= len(actual)
