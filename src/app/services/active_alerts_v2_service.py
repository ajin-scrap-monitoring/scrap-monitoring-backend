"""Read current evaluated alarms without recording, sending or acknowledging alerts."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.alert_policy import AlertPolicy
from src.app.schemas.active_alerts_v2 import ActiveFillAlertPageV2, ActiveFillAlertV2
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.monitoring_v2_service import build_snapshot


async def active_fill_alerts(
    db: AsyncSession, source: MonitoringSource, now: datetime | None = None
) -> ActiveFillAlertPageV2:
    now = now or datetime.now(UTC)
    snapshot = await build_snapshot(db, source, now)
    operations = snapshot.operations
    state = operations.effectiveState if operations else "UNKNOWN"
    result = ActiveFillAlertPageV2(pitId=source.pit_id, computedAt=now, evaluationState=state)
    if operations and operations.evaluationSource == "RECENT_PERSISTED":
        result.reasonCodes = ["OPERATIONS_EVALUATION_PENDING"]
    if operations is None:
        result.reasonCodes = ["OPERATIONS_DISABLED"]
        return result
    if state in ("UNKNOWN", "ERROR"):
        result.reasonCodes = list(snapshot.reasonCodes) or ["CURRENT_OBSERVATION_NOT_EVALUATED"]
        return result
    if state == "NORMAL":
        return result
    if state == "WARNING":
        policy = await db.get(AlertPolicy, source.pit_id)
        if policy and not policy.pre_alert_enabled:
            result.reasonCodes = ["PRE_COLLECTION_ALERT_DISABLED"]
            return result
    # Snapshot freshness/evaluation gates apply even when older notifications remain unread.
    value = snapshot.summary.loadPercent
    if value is None or operations.lastEvaluatedAt is None:
        result.evaluationState = "UNKNOWN"
        result.reasonCodes = ["CURRENT_OBSERVATION_NOT_EVALUATED"]
        return result
    critical = state == "CRITICAL"
    result.items = [
        ActiveFillAlertV2(
            id=f"fill-{'critical' if critical else 'warning'}:{source.pit_id}",
            type="collection_required" if critical else "pre_collection_alert",
            severity="error" if critical else "warning",
            title="스크랩 수거 필요" if critical else "스크랩 적재율 경고",
            evaluatedAt=operations.lastEvaluatedAt,
            measuredAt=snapshot.measuredAt,
            evaluationSource=operations.evaluationSource,
            loadPercent=value,
            simulated=snapshot.summary.simulated,
            # A warning must not link to a previous critical notification.
            notificationId=operations.notificationId if critical else None,
        )
    ]
    return result
