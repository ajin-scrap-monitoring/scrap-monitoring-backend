"""Explicit, resumable derived-summary backfill; never modify observation payloads."""

from datetime import datetime

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.services.compact_history_summary import build_minute_summary

MinuteKey = tuple[str, str, datetime]


async def missing_summaries(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(EdgeV2Minute)
            .where(EdgeV2Minute.history_summary.is_(None))
        )
        or 0
    )


async def backfill_batch(
    db: AsyncSession, batch_size: int, after: MinuteKey | None = None
) -> tuple[int, MinuteKey | None]:
    """Commit at most one small keyset page, skipping rows used by a live writer."""
    if not 1 <= batch_size <= 512:
        raise ValueError("Batch size must be within 1..512")
    minute = EdgeV2Minute
    postgres = db.get_bind().dialect.name == "postgresql"
    if postgres:
        await db.execute(text("SET LOCAL lock_timeout = '1s'"))
        await db.execute(text("SET LOCAL statement_timeout = '5s'"))
    statement = (
        select(minute)
        .where(minute.history_summary.is_(None))
        .order_by(minute.site_id, minute.edge_id, minute.bucket_start)
        .limit(batch_size)
    )
    if after is not None:
        statement = statement.where(
            tuple_(minute.site_id, minute.edge_id, minute.bucket_start) > after
        )
    if postgres:
        statement = statement.with_for_update(skip_locked=True)
    rows = (await db.scalars(statement)).all()
    for row in rows:
        row.history_summary = build_minute_summary(row.bucket_start, row.generated_at, row.payload)
    cursor = (rows[-1].site_id, rows[-1].edge_id, rows[-1].bucket_start) if rows else after
    await db.commit()
    return len(rows), cursor
