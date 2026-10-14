"""Scoped, paginated notification links and immutable collection assessment revisions."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.log import AlertLog
from src.app.schemas.edge_v2 import Assessment
from src.app.schemas.monitoring_v2 import (
    CollectionEventPage,
    MonitoringSource,
    V2Alert,
    V2AlertPage,
)
from src.app.services.edge_v2_service import utc


async def collection_events(
    db: AsyncSession,
    source: MonitoringSource,
    limit: int,
    offset: int,
    assessment_id: str | None = None,
) -> CollectionEventPage:
    """Revision number, never arrival order, selects the latest judgment for each event."""
    artifact = EdgeV2Message
    identity = artifact.payload["assessment_id"].as_string()
    revision = artifact.payload["revision"].as_integer()
    query = select(
        artifact.id,
        func.row_number()
        .over(
            partition_by=identity if assessment_id is None else (identity, revision),
            order_by=(revision.desc(), artifact.id.asc()),
        )
        .label("rank"),
    ).where(
        artifact.site_id == source.site_id,
        artifact.edge_id == source.edge_id,
        artifact.message_type == "edge_assessment",
        artifact.payload["assessment_type"].as_string() == "EVENT",
    )
    if assessment_id is not None:
        query = query.where(identity == assessment_id)
    ranked = query.subquery()
    selection = select(artifact).join(ranked, ranked.c.id == artifact.id)
    selection = selection.where(ranked.c.rank == 1)
    # Filter after ranking so an older COLLECTION revision cannot replace a new judgment.
    selection = selection.where(artifact.payload["event"]["kind"].as_string() == "COLLECTION")
    total = await db.scalar(select(func.count()).select_from(selection.subquery())) or 0
    order = revision.desc() if assessment_id is not None else artifact.id.desc()
    rows = (await db.scalars(selection.order_by(order).offset(offset).limit(limit))).all()
    return CollectionEventPage(
        items=[Assessment.model_validate(row.payload) for row in rows],
        totalItems=total,
        limit=limit,
        offset=offset,
    )


async def alerts(
    db: AsyncSession,
    source: MonitoringSource,
    limit: int,
    offset: int,
) -> V2AlertPage:
    query = (
        select(AlertLog, EdgeV2AlertLink)
        .join(EdgeV2AlertLink, EdgeV2AlertLink.alert_id == AlertLog.id)
        .where(
            EdgeV2AlertLink.pit_id == source.pit_id,
            EdgeV2AlertLink.site_id == source.site_id,
            EdgeV2AlertLink.edge_id == source.edge_id,
        )
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (await db.execute(query.order_by(AlertLog.id.desc()).offset(offset).limit(limit))).all()
    return V2AlertPage(
        items=[
            V2Alert(
                eventId=f"evt-alt-{log.id}",
                notificationId=f"notif-{log.id}",
                assessmentId=link.assessment_id,
                loadPercent=log.fill_ratio_percent,
                observedAt=utc(link.observed_at),
                triggeredAt=utc(log.triggered_at),
                readAt=utc(log.read_at) if log.read_at else None,
            )
            for log, link in rows
        ],
        totalItems=total,
        limit=limit,
        offset=offset,
    )
