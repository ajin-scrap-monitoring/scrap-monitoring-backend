"""Read persisted automatic end judgments for the existing dashboard event contract."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.schemas.history import Event
from src.app.services.collection_detection_service import collection_scope
from src.app.services.edge_v2_service import utc


async def collection_ended_events(
    db: AsyncSession,
    pit_id: str,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> list[Event]:
    """Only stable-end judgments advance the existing elapsed-time display.

    The legacy type/status are compatibility fields, not a human confirmation.
    Original transitions remain the source of truth; no CollectionLog is created.
    """
    source = next(
        (s for s in get_settings().EDGE_V2_MONITORING_SOURCES if s.pit_id == pit_id), None
    )
    if source is None:
        return []
    scope = collection_scope(source, await db.get(PitConfig, pit_id))
    if scope is None:
        return []
    now = datetime.now(UTC)
    end = min(utc(to_time), now) if to_time else now
    stmt = select(EdgeV2Transition).where(
        EdgeV2Transition.site_id == source.site_id,
        EdgeV2Transition.edge_id == source.edge_id,
        EdgeV2Transition.kind == "COLLECTION_SETTLED",
        EdgeV2Transition.payload["pitId"].as_string() == pit_id,
        EdgeV2Transition.payload["scopeToken"].as_string() == scope,
        EdgeV2Transition.occurred_at <= end,
    )
    if from_time:
        stmt = stmt.where(EdgeV2Transition.occurred_at >= utc(from_time))
    rows = (await db.scalars(stmt.order_by(EdgeV2Transition.occurred_at.desc()))).all()
    return [
        Event(
            id=f"evt-auto-col-{row.id}",
            type="collection_completed",
            category="collection",
            severity="info",
            status="completed",
            title="수거 종료 감지",
            detail="높이 감소 후 안정 상태가 유지되어 백엔드가 수거 종료로 판단했습니다.",
            occurredAt=utc(row.occurred_at),
            resolvedAt=utc(row.occurred_at),
            collectionSource="HEIGHT_SETTLED",
            confirmedCompletion=False,
            simulated=row.payload.get("simulated"),
            detectionId=row.payload.get("detectionId"),
        )
        for row in rows
    ]
