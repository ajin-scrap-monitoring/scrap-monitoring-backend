"""Aggregate completion times in SQL without loading collection notes or sensor events."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.models.log import CollectionLog
from src.app.schemas.collection_detection import CollectionTracker, DetectionSummary
from src.app.schemas.collection_summary_v2 import CollectionSummaryV2
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.collection_detection_service import (
    MAX_GAP_SECONDS,
    TRACKER_KIND,
    collection_scope,
)
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
    detection = await detection_summary(db, source, start, now)
    last_settled = detection.lastSettledCollectionAt
    activity_at = max((at for at in (last, last_settled) if at is not None), default=None)
    activity_source = (
        "CONFIRMED_COMPLETION"
        if activity_at is not None and activity_at == last
        else "HEIGHT_SETTLED"
        if activity_at is not None
        else None
    )
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
        lastCollectionActivityAt=activity_at,
        secondsSinceLastCollectionActivity=int((now - activity_at).total_seconds())
        if activity_at
        else None,
        activitySource=activity_source,
        **detection.model_dump(),
    )


async def detection_summary(
    db: AsyncSession, source: MonitoringSource, start: datetime, now: datetime
) -> DetectionSummary:
    settings = get_settings()
    result = DetectionSummary(
        detectionEnabled=settings.EDGE_V2_COLLECTION_DETECTION_ENABLED,
        detectionDropMeters=settings.EDGE_V2_COLLECTION_DROP_METERS,
        detectionConfirmSeconds=settings.EDGE_V2_COLLECTION_CONFIRM_SECONDS,
        detectionSettleSeconds=settings.EDGE_V2_COLLECTION_SETTLE_SECONDS,
    )
    pit = await db.get(PitConfig, source.pit_id)
    scope = collection_scope(source, pit)
    if scope is None:
        result.detectionState = "UNKNOWN" if result.detectionEnabled else "DISABLED"
        result.detectionReasonCodes = ["DETECTION_NOT_CONFIGURED"]
        return result
    event = EdgeV2Transition
    detected = case((event.kind == "COLLECTION_DETECTED", event.occurred_at), else_=None)
    stamp = case((event.occurred_at >= start, detected), else_=None)
    row = (
        await db.execute(
            select(
                func.max(detected).label("last"),
                func.max(
                    case((event.kind == "COLLECTION_SETTLED", event.occurred_at), else_=None)
                ).label("last_settled"),
                func.count(stamp).label("event_count"),
                func.min(stamp).label("first_in_window"),
                func.max(stamp).label("last_in_window"),
            ).where(
                event.site_id == source.site_id,
                event.edge_id == source.edge_id,
                event.kind.in_(["COLLECTION_DETECTED", "COLLECTION_SETTLED"]),
                event.occurred_at <= now,
                event.payload["pitId"].as_string() == source.pit_id,
                event.payload["scopeToken"].as_string() == scope,
            )
        )
    ).one()
    if row.last_settled is not None:
        result.lastSettledCollectionAt = utc(row.last_settled)
        result.secondsSinceLastSettlement = int((now - utc(row.last_settled)).total_seconds())
    if row.last is not None:
        result.lastDetectedCollectionAt = utc(row.last)
        result.secondsSinceLastDetection = int((now - utc(row.last)).total_seconds())
    result.detectedCollectionsInWindow = int(row.event_count)
    if row.event_count >= 2:
        result.averageDetectedCollectionCycleSeconds = (
            utc(row.last_in_window) - utc(row.first_in_window)
        ).total_seconds() / (row.event_count - 1)
    tracker = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    state = CollectionTracker.model_validate(tracker.payload) if tracker else None
    if state and state.scope_token == scope and state.pit_id == source.pit_id:
        result.detectionSimulated = state.simulated
    if not result.detectionEnabled:
        result.detectionReasonCodes = ["DETECTION_DISABLED"]
    elif (
        state
        and state.scope_token == scope
        and state.pit_id == source.pit_id
        and (
            state.input_valid
            and timedelta(0)
            <= now - utc(state.observed_at)
            < timedelta(seconds=min(MAX_GAP_SECONDS, settings.EDGE_V2_ASSESSMENT_TTL_SECONDS))
        )
    ):
        result.detectionState = state.phase
    else:
        result.detectionState = "UNKNOWN"
        result.detectionReasonCodes = ["DETECTION_INPUT_UNAVAILABLE"]
    return result
