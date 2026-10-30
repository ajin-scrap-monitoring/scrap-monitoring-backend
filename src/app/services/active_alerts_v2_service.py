"""Read current evaluated alarms without recording, sending or acknowledging alerts."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.alert_policy import AlertPolicy
from src.app.schemas.active_alerts_v2 import (
    ActiveAlertBasisV2,
    ActiveFillAlertPageV2,
    ActiveFillAlertV2,
)
from src.app.schemas.monitoring_v2 import MonitoringSnapshotV2, MonitoringSource
from src.app.services.active_health_service import apply_starts, health_conditions
from src.app.services.monitoring_v2_service import build_snapshot


async def active_fill_alerts(
    db: AsyncSession,
    source: MonitoringSource,
    now: datetime | None = None,
    *,
    include_health: bool = False,
) -> ActiveFillAlertPageV2:
    now = now or datetime.now(UTC)
    snapshot = await build_snapshot(db, source, now)
    result = await _fill_alerts(db, source, snapshot, now)
    if include_health:
        result.scope = "ALL"
        health, result.coverage = health_conditions(snapshot, now)
        result.items.extend(health)
    await apply_starts(db, source, snapshot, result.items, now)
    return result


async def _fill_alerts(
    db: AsyncSession, source: MonitoringSource, snapshot: MonitoringSnapshotV2, now: datetime
) -> ActiveFillAlertPageV2:
    operations = snapshot.operations
    state = operations.effectiveState if operations else "UNKNOWN"
    result = ActiveFillAlertPageV2(
        pitId=source.pit_id,
        computedAt=now,
        evaluationState=state,
        basis=ActiveAlertBasisV2(
            snapshotId=snapshot.snapshotId,
            dataState=snapshot.dataState,
            filterState=snapshot.summary.filterState,
            inputSource=snapshot.summary.inputSource,
            measuredAt=snapshot.measuredAt,
            lastEvaluatedAt=operations.lastEvaluatedAt if operations else None,
            evaluationSource=operations.evaluationSource if operations else None,
            lastKnownState=operations.lastKnownState if operations else None,
            reasonCodes=list(snapshot.reasonCodes),
        ),
    )
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
