"""Aggregate completion times in SQL without loading collection notes or sensor events."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.log import CollectionLog
from src.app.schemas.collection_summary_v2 import CollectionSummaryV2
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.edge_v2_service import utc


async def collection_summary(
    db: AsyncSession,
    source: MonitoringSource,
    window_days: int = 30,
    now: datetime | None = None,
) -> CollectionSummaryV2:
    """The mean of consecutive intervals is (last - first) / (count - 1).

    Only intervals whose two completions fall inside the requested window count.
    The last completion may predate that window so its elapsed time stays useful.
    A received COLLECTION judgment is evidence, not a confirmed completion.
    """
    now = now or datetime.now(UTC)
    start = now - timedelta(days=window_days)
    completed = CollectionLog.completed_at
    in_window = case((completed >= start, completed), else_=None)
    row = (
        await db.execute(
            select(
                func.max(completed).label("last"),
                func.count(in_window).label("completion_count"),
                func.min(in_window).label("first_in_window"),
                func.max(in_window).label("last_in_window"),
            ).where(CollectionLog.pit_id == source.pit_id, completed <= now)
        )
    ).one()
    last = utc(row.last) if row.last is not None else None
    count = int(row.completion_count)
    cycles = max(0, count - 1)
    average = (
        (utc(row.last_in_window) - utc(row.first_in_window)).total_seconds() / cycles
        if cycles
        else None
    )
    reasons = []
    if last is None:
        reasons.append("NO_CONFIRMED_COLLECTIONS")
    if not cycles:
        reasons.append("INSUFFICIENT_COLLECTIONS_IN_WINDOW")
    return CollectionSummaryV2(
        pitId=source.pit_id,
        computedAt=now,
        windowFrom=start,
        windowTo=now,
        lastCollectionAt=last,
        secondsSinceLastCollection=int((now - last).total_seconds()) if last else None,
        completedCollectionsInWindow=count,
        cycleCount=cycles,
        averageCollectionCycleSeconds=average,
        reasonCodes=reasons,
    )
