"""Bounded spreadsheet exports using the existing history judgments, without files on disk."""

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.schemas.history import CollectionScope, Event
from src.app.schemas.load_history_v2 import HistoryInterval, HistorySampleV2
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.history_service import history_service
from src.app.services.load_history_v2_service import history_window, load_history

MAX_EVENT_CANDIDATES = 10000
MAX_CSV_BYTES = 8 * 1024 * 1024
QUERY_SECONDS = 20


def cell(value: Any) -> str:
    """Preserve null/zero and CSV structure; make formula-like text explicit text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        stripped = value.lstrip(" \t\r\n\ufeff")
        if value.startswith(("\t", "\r", "\n")) or stripped.startswith(
            ("=", "+", "-", "@", "＝", "＋", "－", "＠")
        ):
            return "'" + value
        return value
    return str(value)


async def encode_csv(columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    """Complete the bounded response before sending headers; never silently truncate."""
    output = bytearray(b"\xef\xbb\xbf")
    line = io.StringIO(newline="")
    writer = csv.writer(line, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(columns)
    output.extend(line.getvalue().encode("utf-8"))
    if len(output) > MAX_CSV_BYTES:
        raise HTTPException(422, "CSV_SIZE_LIMIT_EXCEEDED")
    for number, row in enumerate(rows):
        line.seek(0)
        line.truncate()
        writer.writerow([cell(row.get(key)) for key in columns])
        encoded = line.getvalue().encode("utf-8")
        if len(output) + len(encoded) > MAX_CSV_BYTES:
            raise HTTPException(422, "CSV_SIZE_LIMIT_EXCEEDED")
        output.extend(encoded)
        if number % 128 == 0:
            await asyncio.sleep(0)
    return bytes(output)


@asynccontextmanager
async def export_transaction(db: AsyncSession) -> AsyncIterator[None]:
    """Release authentication reads; pin one PG snapshot and always release on cancellation."""
    await db.rollback()
    try:
        async with asyncio.timeout(QUERY_SECONDS):
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                await db.execute(
                    text("SELECT set_config('statement_timeout', :budget, true)"),
                    {"budget": str(int(QUERY_SECONDS * 1000))},
                )
            yield
    except TimeoutError:
        raise HTTPException(503, "CSV_QUERY_TIMEOUT") from None
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014":
            raise HTTPException(503, "CSV_QUERY_TIMEOUT") from None
        raise
    finally:
        await db.rollback()


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def load_csv(
    db: AsyncSession,
    source: MonitoringSource,
    start: datetime | None,
    end: datetime | None,
    interval: HistoryInterval | None,
) -> tuple[bytes, int]:
    async with export_transaction(db):
        history = await load_history(db, source, start, end, interval)
        context = {
            "pitId": history.pitId,
            "siteId": history.source.site_id,
            "edgeId": history.source.edge_id,
            "from": stamp(history.from_time),
            "to": stamp(history.to_time),
            "bucketSeconds": history.bucketSeconds,
            "selectionPolicy": history.selectionPolicy,
        }
        columns = [*context, *HistorySampleV2.model_fields]
        body = await encode_csv(
            columns,
            (context | sample.model_dump(mode="json") for sample in history.samples),
        )
        return body, len(history.samples)


async def candidate_count(db: AsyncSession, query: Select[Any]) -> int:
    """Count a limited ID subquery, not full payloads or an unbounded table count."""
    value = await db.scalar(
        select(func.count()).select_from(query.limit(MAX_EVENT_CANDIDATES + 1).subquery())
    )
    return int(value or 0)


async def guard_event_volume(
    db: AsyncSession, source: MonitoringSource, start: datetime, end: datetime, category: str
) -> None:
    """Conservatively cap input candidates before the existing event assembler loads them."""
    pit = source.pit_id
    queries: list[Select[Any]] = []
    if category in ("all", "collection"):
        queries.append(
            select(CollectionLog.id).where(
                CollectionLog.pit_id == pit,
                CollectionLog.completed_at >= start,
                CollectionLog.completed_at <= end,
            )
        )
    if category in ("all", "alert"):
        queries.append(
            select(AlertLog.id).where(
                AlertLog.pit_id == pit, AlertLog.triggered_at >= start, AlertLog.triggered_at <= end
            )
        )
    if category in ("all", "error"):
        queries.append(
            select(ScrapMetric.id).where(
                ScrapMetric.pit_id == pit,
                ScrapMetric.is_valid.is_(False),
                ScrapMetric.measured_at >= start,
                ScrapMetric.measured_at <= end,
            )
        )
    event = EdgeV2Transition
    recorded_pit = event.payload["pitId"].as_string()
    conditions = []
    if category in ("all", "collection"):
        conditions.append((event.kind == "COLLECTION_SETTLED") & (recorded_pit == pit))
    if category in ("all", "error"):
        conditions.append(
            event.kind.in_(["DEVICE_STATE_CHANGED", "MEASUREMENT_VALIDITY_CHANGED"])
            & event.payload["diagnosticOnly"].as_boolean().is_not(True)
            & or_(recorded_pit == pit, recorded_pit.is_(None))
        )
    if conditions:
        queries.append(
            select(event.id).where(
                event.site_id == source.site_id,
                event.edge_id == source.edge_id,
                event.occurred_at >= start,
                event.occurred_at <= end,
                or_(*conditions),
            )
        )
    total = 0
    for query in queries:
        total += await candidate_count(db, query)
        if total > MAX_EVENT_CANDIDATES:
            raise HTTPException(422, "CSV_EVENT_LIMIT_EXCEEDED")
    # Existing event assembly also reads clip associations across this pit, independently
    # of the selected time range. Bound that input separately without changing its semantics.
    jobs = select(MediaClipJob.id).where(
        MediaClipJob.pit_id == pit, MediaClipJob.clip_id.is_not(None)
    )
    if await candidate_count(db, jobs) > MAX_EVENT_CANDIDATES:
        raise HTTPException(422, "CSV_REFERENCE_LIMIT_EXCEEDED")


async def events_csv(
    db: AsyncSession,
    source: MonitoringSource,
    start: datetime | None,
    end: datetime | None,
    category: str,
    collection_scope: CollectionScope,
) -> tuple[bytes, int]:
    start, end, _ = history_window(start, end, None, datetime.now(UTC))
    async with export_transaction(db):
        await guard_event_volume(db, source, start, end, category)
        page = await history_service.list_events(
            source.pit_id, start, end, category, 1, MAX_EVENT_CANDIDATES, db, collection_scope
        )
        if page.page.totalItems > MAX_EVENT_CANDIDATES:
            raise HTTPException(422, "CSV_EVENT_LIMIT_EXCEEDED")
        context = {
            "pitId": source.pit_id,
            "from": stamp(start),
            "to": stamp(end),
            "categoryFilter": category,
            "collectionScope": collection_scope,
        }
        body = await encode_csv(
            [*context, *Event.model_fields],
            (context | item.model_dump(mode="json") for item in page.items),
        )
        return body, len(page.items)
