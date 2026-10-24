"""Read recorded device/input changes; do not infer present health or matched error episodes."""

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.schemas.history import Event
from src.app.services.edge_v2_service import utc

PREPARING_REASONS = {"SCAN_RECOVERING", "FILTER_WARMING", "MEDIAN_WARMING", "INITIALIZING"}


def error_transition_event(row: EdgeV2Transition) -> Event | None:
    """Legacy status describes the occurrence only; state and reasons preserve its meaning."""
    data = row.payload
    raw_reasons = data.get("reasonCodes")
    reasons = (
        [reason for reason in raw_reasons if isinstance(reason, str)]
        if isinstance(raw_reasons, list)
        else []
    )
    preparing = bool(PREPARING_REASONS.intersection(reasons))
    kind = row.kind
    if kind == "DEVICE_STATE_CHANGED":
        previous, current = data.get("previous"), data.get("current")
        if previous == current:
            return None
        if current in ("MEASUREMENT_UNAVAILABLE", "CONFIG_ERROR"):
            state, severity = "UNAVAILABLE", "error"
            title = "장치 측정 중단" if current == "MEASUREMENT_UNAVAILABLE" else "장치 설정 오류"
        elif current == "HEALTHY":
            state, severity, title = "RECOVERED", "info", "장치 상태 복구"
        elif current == "DEGRADED":
            state = "PREPARING" if preparing else "DEGRADED"
            severity = "info" if preparing else "warning"
            title = "장치 측정 재준비" if preparing else "장치 성능 저하"
        elif current == "UNKNOWN":
            state, severity, title = "UNKNOWN", "warning", "장치 상태 확인 불가"
        else:
            return None
        detail = f"장치 보고 상태가 {previous}에서 {current}(으)로 변경되었습니다."
    elif kind == "MEASUREMENT_VALIDITY_CHANGED":
        previous, current = data.get("previousValid"), data.get("currentValid")
        if not isinstance(previous, bool) or not isinstance(current, bool) or previous == current:
            return None
        if current:
            state, severity, title = "RECOVERED", "info", "측정 입력 복구"
            detail = "유효한 측정 입력이 다시 확인되었습니다. 필터 준비 완료를 의미하지 않습니다."
        elif preparing:
            state, severity, title = "PREPARING", "info", "측정 입력 준비"
            detail = "측정 입력을 준비 중이라는 상태 변화가 기록되었습니다."
        else:
            state, severity, title = "UNAVAILABLE", "warning", "측정 입력 사용 불가"
            detail = "계산에 사용할 유효한 측정 입력이 확인되지 않았습니다."
    else:
        return None
    if reasons:
        detail += " 사유: " + ", ".join(reasons)
    detail += " 기록 당시 상태이며 현재 장치 상태나 다른 오류의 해제 여부를 뜻하지 않습니다."
    occurred = utc(row.occurred_at)
    return Event(
        id=f"evt-v2-transition-{row.id}",
        type="device_error" if kind == "DEVICE_STATE_CHANGED" else "measurement_error",
        category="error",
        severity=severity,
        status="resolved" if state == "RECOVERED" else "active",
        title=title,
        detail=detail,
        occurredAt=occurred,
        resolvedAt=occurred if state == "RECOVERED" else None,
        eventSource="EDGE_V2_TRANSITION",
        transitionId=row.id,
        transitionKind=kind,
        stateAtOccurrence=state,
        statusBasis="AT_OCCURRENCE",
        pitAttribution="RECORDED" if data.get("pitId") else "CURRENT_ASSIGNMENT",
        reasonCodes=reasons,
        simulated=data.get("simulated") if isinstance(data.get("simulated"), bool) else None,
    )


async def error_history_events(
    db: AsyncSession,
    pit_id: str,
    from_time: datetime | None,
    to_time: datetime | None,
    now: datetime | None = None,
) -> list[Event]:
    source = next(
        (s for s in get_settings().EDGE_V2_MONITORING_SOURCES if s.pit_id == pit_id), None
    )
    if source is None:
        return []
    end = now or datetime.now(UTC)
    if to_time is not None:
        end = min(end, utc(to_time))
    event = EdgeV2Transition
    recorded_pit = event.payload["pitId"].as_string()
    query = select(event).where(
        event.site_id == source.site_id,
        event.edge_id == source.edge_id,
        event.kind.in_(["DEVICE_STATE_CHANGED", "MEASUREMENT_VALIDITY_CHANGED"]),
        or_(recorded_pit == pit_id, recorded_pit.is_(None)),
        event.occurred_at <= end,
    )
    if from_time is not None:
        query = query.where(event.occurred_at >= utc(from_time))
    rows = await db.scalars(query.order_by(event.occurred_at.desc(), event.id.desc()))
    result = []
    for row in rows:
        item = error_transition_event(row)
        if item is not None:
            result.append(item)
    return result
