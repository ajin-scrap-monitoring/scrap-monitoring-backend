"""Evidence-backed current conditions and bounded server-evaluation start pointers."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.alert_policy import AlertPolicy
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.active_alerts_v2 import (
    ActiveFillAlertV2,
    ActiveHealthAlertV2,
    ActiveHealthCoverageV2,
)
from src.app.schemas.monitoring_v2 import MonitoringSnapshotV2, MonitoringSource
from src.app.services.alert_lifecycle_service import lifecycle_scope, stamp
from src.app.services.edge_v2_service import digest
from src.app.services.error_history_service import PREPARING_REASONS

TRACKER_KIND = "active_alert_starts"
Alert = ActiveFillAlertV2 | ActiveHealthAlertV2


def health_conditions(
    snapshot: MonitoringSnapshotV2, now: datetime
) -> tuple[list[ActiveHealthAlertV2], list[ActiveHealthCoverageV2]]:
    """A stale status cannot assert a current physical fault or recovery."""
    device, source = snapshot.deviceStatus, snapshot.source
    report = device.snapshot
    items: list[ActiveHealthAlertV2] = []
    coverage = [
        ActiveHealthCoverageV2(
            componentKind="CAMERA",
            state="UNSUPPORTED",
            reasonCodes=["CAMERA_HEALTH_SOURCE_NOT_CONFIGURED"],
        )
    ]
    configured: tuple[str, ...] = next(
        (
            d.sensor_ids
            for d in get_settings().EDGE_V2_DEVICES
            if d.site_id == source.site_id and d.edge_id == source.edge_id
        ),
        (),
    )
    fresh = (
        device.connection_state == "REPORTING"
        and report is not None
        and timedelta(seconds=-5) <= now - report.generated_at < timedelta(seconds=30)
    )

    def condition(
        kind: str,
        identifier: str,
        state: str,
        title: str,
        reasons: list[str],
        evidence: str,
        measured: datetime | None,
        severe: bool = False,
    ) -> None:
        items.append(
            ActiveHealthAlertV2(
                id=f"health:{kind.lower()}:{source.pit_id}:{identifier}",
                type="measurement_error" if kind == "MEASUREMENT" else "device_error",
                severity="error" if severe else "warning",
                title=title,
                componentKind=kind,
                componentId=identifier,
                state=state,
                evaluatedAt=now,
                measuredAt=measured,
                evidenceSource=evidence,
                reasonCodes=list(dict.fromkeys(reasons)),
                simulated=snapshot.summary.simulated if kind == "MEASUREMENT" else None,
            )
        )

    if not fresh:
        condition(
            "EDGE",
            source.edge_id,
            "STATUS_UNKNOWN",
            "Edge 상태 확인 불가",
            [
                "STATUS_NOT_RECEIVED"
                if device.connection_state != "REPORTING"
                else "STATUS_TIME_UNTRUSTED"
            ],
            "STATUS_FRESHNESS",
            device.last_received_at,
        )
        for kind, identifier in [
            ("EDGE", source.edge_id),
            *[("LIDAR", s) for s in configured],
            ("MEASUREMENT", source.pit_id),
        ]:
            coverage.append(
                ActiveHealthCoverageV2(
                    componentKind=kind,
                    componentId=identifier,
                    state="UNKNOWN",
                    reasonCodes=["CURRENT_STATUS_UNAVAILABLE"],
                )
            )
        return items, coverage
    assert report is not None
    coverage.append(
        ActiveHealthCoverageV2(componentKind="EDGE", componentId=source.edge_id, state="CHECKED")
    )
    preparing = bool(PREPARING_REASONS.intersection(report.reason_codes))
    if report.overall_state != "HEALTHY" and not (preparing and report.overall_state == "DEGRADED"):
        condition(
            "EDGE",
            source.edge_id,
            report.overall_state,
            "Edge 장치 상태 이상",
            report.reason_codes,
            "EDGE_STATUS",
            report.generated_at,
            report.overall_state in ("MEASUREMENT_UNAVAILABLE", "CONFIG_ERROR"),
        )
    by_sensor = {s.sensor_id: s for s in report.sensors}
    for identifier in configured:
        sensor = by_sensor.get(identifier)
        state = sensor.data_state if sensor else "UNKNOWN"
        reasons = list(sensor.reason_codes) if sensor else ["SENSOR_STATUS_NOT_RECEIVED"]
        measured = sensor.last_scan_at if sensor else None
        if state == "FRESH" and (measured is None or measured > now):
            state = "UNKNOWN"
            reasons.append("SCAN_TIME_UNAVAILABLE")
        elif (
            state == "FRESH"
            and measured is not None
            and now - measured >= timedelta(seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
        ):
            state = "STALE"
            reasons.append("SCAN_STALE")
        preparing = state == "RECOVERING" or bool(PREPARING_REASONS.intersection(reasons))
        coverage.append(
            ActiveHealthCoverageV2(
                componentKind="LIDAR",
                componentId=identifier,
                state="CHECKED"
                if state in ("FRESH", "STALE", "UNAVAILABLE") and not preparing
                else "UNKNOWN",
                reasonCodes=reasons,
            )
        )
        if state != "FRESH" and not preparing:
            condition(
                "LIDAR",
                identifier,
                state,
                "LiDAR 측정 중단" if state == "UNAVAILABLE" else "LiDAR 상태 확인 필요",
                reasons,
                "EDGE_STATUS",
                measured,
                state == "UNAVAILABLE",
            )
    preparing = bool(PREPARING_REASONS.intersection(snapshot.reasonCodes)) or any(
        reason in snapshot.reasonCodes for reason in ("FILTER_WARMING", "FILTER_DISABLED")
    )
    measurement_state = snapshot.dataState
    coverage.append(
        ActiveHealthCoverageV2(
            componentKind="MEASUREMENT",
            componentId=source.pit_id,
            state="CHECKED"
            if measurement_state in ("FRESH", "UNAVAILABLE", "STALE") and not preparing
            else "UNKNOWN",
            reasonCodes=snapshot.reasonCodes,
        )
    )
    if measurement_state != "FRESH" and not preparing:
        condition(
            "MEASUREMENT",
            source.pit_id,
            measurement_state,
            "측정 데이터 확인 필요",
            snapshot.reasonCodes or ["MEASUREMENT_UNAVAILABLE"],
            "CURRENT_MEASUREMENT",
            snapshot.measuredAt,
            measurement_state == "UNAVAILABLE",
        )
    return items, coverage


def signature(snapshot: MonitoringSnapshotV2, identifier: str, state: str) -> str:
    report = snapshot.deviceStatus.snapshot
    scope = (
        lifecycle_scope(snapshot)
        if identifier.startswith("fill-") or ":measurement:" in identifier
        else digest(
            [
                snapshot.source.model_dump(),
                report.config_revision if report else None,
                report.orchestrator_instance_id if report else None,
                # Missing status episodes cannot span a newly received heartbeat.
                snapshot.deviceStatus.last_received_at.isoformat()
                if state == "STATUS_UNKNOWN" and snapshot.deviceStatus.last_received_at
                else None,
            ]
        )
    )
    return digest([identifier, state, scope])


async def record_starts(
    db: AsyncSession, snapshot: MonitoringSnapshotV2, now: datetime, fill_state: str | None
) -> bool:
    """Called only by the existing worker transaction; GET never creates a start time."""
    source = snapshot.source
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    old = row.payload if row and row.payload.get("pitId") == source.pit_id else {}
    before = stamp(old.get("evaluatedAt"))
    continuous = before is not None and timedelta(0) <= now - before < timedelta(
        seconds=min(30, get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
    )
    conditions = {item.id: item.state for item in health_conditions(snapshot, now)[0]}
    if fill_state in ("WARNING", "CRITICAL") and snapshot.dataState == "FRESH":
        policy = await db.get(AlertPolicy, source.pit_id)
        if fill_state == "CRITICAL" or not policy or policy.pre_alert_enabled:
            conditions[
                f"fill-{'critical' if fill_state == 'CRITICAL' else 'warning'}:{source.pit_id}"
            ] = fill_state
    entries = {}
    for identifier, state in conditions.items():
        token = signature(snapshot, identifier, state)
        previous = old.get("entries", {}).get(identifier, {}) if continuous else {}
        start = stamp(previous.get("startedAt")) if previous.get("signature") == token else None
        entries[identifier] = {"signature": token, "startedAt": (start or now).isoformat()}
    payload = {"pitId": source.pit_id, "evaluatedAt": now.isoformat(), "entries": entries}
    if row is not None and row.payload == payload:
        return False
    if row is None:
        row = EdgeV2Latest(site_id=source.site_id, edge_id=source.edge_id, kind=TRACKER_KIND)
        db.add(row)
    row.message_id = snapshot.snapshotId
    row.generated_at = now
    row.payload = payload
    return True


async def apply_starts(
    db: AsyncSession,
    source: MonitoringSource,
    snapshot: MonitoringSnapshotV2,
    items: list[Alert],
    now: datetime,
) -> None:
    if not items:
        return
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    if row is None or row.payload.get("pitId") != source.pit_id:
        return
    before = stamp(row.payload.get("evaluatedAt"))
    if before is None or not timedelta(0) <= now - before < timedelta(
        seconds=min(30, get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
    ):
        return
    for item in items:
        state = (
            item.state
            if isinstance(item, ActiveHealthAlertV2)
            else ("CRITICAL" if item.type == "collection_required" else "WARNING")
        )
        evidence = row.payload.get("entries", {}).get(item.id, {})
        if evidence.get("signature") == signature(snapshot, item.id, state):
            started = stamp(evidence.get("startedAt"))
            if started is not None and started <= before:
                item.startedAt = started
                item.startBasis = "SERVER_EVALUATION"
