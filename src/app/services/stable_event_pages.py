"""Freeze event metadata once, then page without consulting mutable event rows again."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.schemas.event_recording import EventRecording, EventRecordingPage
from src.app.schemas.history import PageMetadata
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services import event_recording_service as live
from src.app.services.edge_v2_service import utc
from src.app.services.event_page_store import EventPageStore, FrozenEventList, PageContext
from src.app.services.event_recording_queries import (
    active_collection,
    event_table,
    playback_scope,
    reference,
    shifted,
)

MAX_ITEMS = 10_000
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
QUERY_SECONDS = 20


def _utc(value: datetime) -> datetime:
    try:
        return utc(value)
    except (OverflowError, ValueError):
        raise HTTPException(422, "EVENT_LIST_TIME_OUT_OF_RANGE") from None


async def stable_event_page(
    db: AsyncSession,
    store: EventPageStore,
    owner: str,
    token: str | None,
    pit_id: str | None,
    category: str,
    from_time: datetime | None,
    to_time: datetime | None,
    page: int,
    page_size: int,
    before: int,
    after: int,
    include_playback: bool,
) -> EventRecordingPage:
    live._context(before, after)
    pit, source = playback_scope(pit_id)
    settings = get_settings()
    binding = hashlib.sha256(
        json.dumps(
            [
                pit,
                source.model_dump() if source else None,
                settings.MEDIA_CAMERA_ID,
                settings.MEDIA_SERVICE_URL,
                settings.MEDIA_DAILY_SERVICE_URL,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if token:
        saved = store.get(token, owner)
        ctx = saved.context
        if ctx.binding != binding:
            raise HTTPException(409, "EVENT_LIST_SOURCE_CHANGED")
        if (
            (category, page_size, before, after)
            != (ctx.category, ctx.page_size, ctx.before, ctx.after)
            or (from_time is not None and _utc(from_time) != ctx.lower)
            or (to_time is not None and _utc(to_time) != ctx.upper)
        ):
            raise HTTPException(409, "EVENT_LIST_QUERY_CHANGED")
    else:
        if page != 1:
            raise HTTPException(422, "EVENT_LIST_START_AT_FIRST_PAGE")
        now = datetime.now(UTC)
        upper = _utc(to_time) if to_time else now
        lower = _utc(from_time) if from_time else shifted(upper, -7 * 86400)
        if lower >= upper or upper - lower > timedelta(days=90):
            raise HTTPException(422, "Use an increasing range of at most 90 days")
        ctx = PageContext(pit, binding, category, page_size, before, after, lower, upper, now)
        store.begin()
        try:
            items = await _freeze(db, ctx, source)
            saved = store.put(owner, ctx, items, datetime.now(UTC))
        finally:
            store.end()
    return await _page(saved, page, include_playback)


async def _freeze(
    db: AsyncSession, ctx: PageContext, source: MonitoringSource | None
) -> list[bytes]:
    # The single SELECT owns the initial row set, even when commits arrive out of ID order.
    items: list[bytes] = []
    size = 1024
    try:
        async with asyncio.timeout(QUERY_SECONDS):
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(text("SELECT set_config('statement_timeout', '20000', true)"))
            active = await active_collection(db, source, ctx.as_of)
            table = event_table(
                db,
                ctx.pit,
                source,
                ctx.category,
                ctx.lower,
                ctx.upper,
                ctx.as_of,
                ctx.before,
                ctx.after,
                active,
            )
            result = await db.stream(
                select(table)
                .order_by(table.c.at.desc(), table.c.event_id.desc())
                .limit(MAX_ITEMS + 1),
                execution_options={"yield_per": 64},
            )
            try:
                async for row in result.mappings():
                    if len(items) >= MAX_ITEMS:
                        raise HTTPException(422, "EVENT_LIST_SNAPSHOT_TOO_LARGE")
                    encoded = (
                        reference(row, ctx.pit, ctx.before, ctx.after, active)
                        .model_dump_json()
                        .encode()
                    )
                    size += len(encoded) + 64
                    if size > MAX_SNAPSHOT_BYTES:
                        raise HTTPException(422, "EVENT_LIST_SNAPSHOT_TOO_LARGE")
                    items.append(encoded)
            finally:
                await result.close()
    except TimeoutError:
        await db.rollback()
        raise HTTPException(503, "EVENT_LIST_QUERY_TIMEOUT") from None
    except SQLAlchemyError as exc:
        await db.rollback()
        code = (
            "EVENT_LIST_QUERY_TIMEOUT"
            if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014"
            else "EVENT_LIST_STORAGE_UNAVAILABLE"
        )
        raise HTTPException(503, code) from None
    return items


async def _page(saved: FrozenEventList, page: int, include_playback: bool) -> EventRecordingPage:
    ctx = saved.context
    start = (page - 1) * ctx.page_size
    items = [
        EventRecording.model_validate_json(raw)
        for raw in saved.items[start : start + ctx.page_size]
    ]
    if include_playback and items:
        from src.app.services.event_playback_summary import add_playback_summaries

        await add_playback_summaries(items, ctx.as_of)
    total = len(saved.items)
    return EventRecordingPage(
        items=items,
        page=PageMetadata(
            page=page,
            pageSize=ctx.page_size,
            totalItems=total,
            totalPages=(total + ctx.page_size - 1) // ctx.page_size,
        ),
        from_=ctx.lower,
        to=ctx.upper,
        asOf=ctx.as_of,
        snapshotToken=saved.token,
        snapshotExpiresAt=saved.expires_at,
    )
