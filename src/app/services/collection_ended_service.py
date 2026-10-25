"""Read persisted automatic end judgments for the existing dashboard event contract."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.schemas.history import CollectionCriteria, Event
from src.app.services.collection_detection_service import collection_scope
from src.app.services.edge_v2_service import utc


async def collection_ended_events(
    db: AsyncSession,
    pit_id: str,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    *,
    include_previous_scopes: bool = False,
) -> list[Event]:
    """Only persisted automatic end judgments advance the existing elapsed-time display.

    The legacy type/status are compatibility fields, not a human confirmation.
    Original transitions remain the source of truth; no CollectionLog is created.
    The explicit historical option includes earlier criteria without changing the default clock.
    """
    source = next(
        (s for s in get_settings().EDGE_V2_MONITORING_SOURCES if s.pit_id == pit_id), None
    )
    if source is None:
        return []
    scope = collection_scope(source, await db.get(PitConfig, pit_id))
    if scope is None and not include_previous_scopes:
        return []
    now = datetime.now(UTC)
    end = min(utc(to_time), now) if to_time else now
    stmt = select(EdgeV2Transition).where(
        EdgeV2Transition.site_id == source.site_id,
        EdgeV2Transition.edge_id == source.edge_id,
        EdgeV2Transition.kind == "COLLECTION_SETTLED",
        EdgeV2Transition.payload["pitId"].as_string() == pit_id,
        EdgeV2Transition.occurred_at <= end,
    )
    if not include_previous_scopes:
        stmt = stmt.where(EdgeV2Transition.payload["scopeToken"].as_string() == scope)
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
            detail="높이 감소 후 일정 시간 추가 감소가 확인되지 않아 수거 종료로 판단했습니다.",
            occurredAt=utc(row.occurred_at),
            resolvedAt=utc(row.occurred_at),
            collectionSource="HEIGHT_SETTLED",
            confirmedCompletion=False,
            simulated=row.payload.get("simulated"),
            detectionId=row.payload.get("detectionId"),
            collectionCriteria=retained_criteria(row, scope),
        )
        for row in rows
    ]


def retained_criteria(row: EdgeV2Transition, current_scope: str | None) -> CollectionCriteria:
    """Comparisons can be unknown if the original or current criteria are unavailable."""
    payload = row.payload
    strings = {
        key: value if isinstance(value := payload.get(key), str) else None
        for key in (
            "scopeToken",
            "detectionVersion",
            "heightBasis",
            "detectionProfile",
            "endPolicy",
        )
    }
    seconds = payload.get("settleSeconds")
    return CollectionCriteria(
        **strings,
        matchesCurrentScope=strings["scopeToken"] == current_scope
        if strings["scopeToken"] is not None and current_scope is not None
        else None,
        settleSeconds=seconds
        if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0
        else None,
    )
