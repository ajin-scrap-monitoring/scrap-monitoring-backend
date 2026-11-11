"""Operational scan alarms, independent of raw status and calculation validity."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.edge_v2 import ScanUpdateAlarm, SensorStatus, StatusReport

SCAN_REASONS = {"SCAN_NOT_UPDATED", "SCAN_RECOVERING"}


def known_alarm(sensor: SensorStatus) -> ScanUpdateAlarm | None:
    alarm = sensor.scan_update_alarm
    if (
        alarm is None
        or alarm.state == "UNKNOWN"
        or sensor.data_state not in ("FRESH", "STALE", "RECOVERING")
        or not set(sensor.reason_codes) <= SCAN_REASONS
        or sensor.last_scan_at is None
        or alarm.last_valid_scan_at != sensor.last_scan_at
    ):
        return None
    return alarm


def managed_report(report: StatusReport) -> bool:
    return len(report.sensors) == 2 and all(known_alarm(s) is not None for s in report.sensors)


def operational_device(report: StatusReport) -> tuple[str, list[str]]:
    """Remove only supported scan-delay causes; retain faults and loss warnings."""
    if not managed_report(report) or any(s.state == "FATAL" for s in report.services):
        return report.overall_state, report.reason_codes
    reasons = [r for r in report.reason_codes if r not in SCAN_REASONS]
    if report.overall_state in ("HEALTHY", "DEGRADED", "MEASUREMENT_UNAVAILABLE"):
        if not reasons or set(reasons) <= {"OUTBOX_DATA_LOSS"}:
            state = (
                "DEGRADED"
                if reasons or any(s.state != "HEALTHY" for s in report.services)
                else "HEALTHY"
            )
            return state, reasons
    return report.overall_state, report.reason_codes


async def record_scan_alarms(db: AsyncSession, report: StatusReport) -> None:
    """Persist bounded episode state; missing/unknown reports never clear an alarm."""
    key = (report.site_id, report.edge_id, "scan_update_alarms")
    row = await db.get(EdgeV2Latest, key)
    if row is None and not any(s.scan_update_alarm is not None for s in report.sensors):
        return
    scope = [report.orchestrator_instance_id, report.config_revision]
    previous = row.payload if row and row.payload.get("scope") == scope else {}
    entries = dict(previous.get("entries", {}))
    pit = next(
        (
            s.pit_id
            for s in get_settings().EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (report.site_id, report.edge_id)
        ),
        None,
    )
    for sensor in report.sensors:
        alarm = known_alarm(sensor)
        if alarm is None:
            continue
        old = entries.get(sensor.sensor_id)
        if old is True and alarm.state == "PENDING":
            # A pending report is not a recovery of an already confirmed episode.
            continue
        if (alarm.active is True and old is not True) or (alarm.active is False and old is True):
            db.add(
                EdgeV2Transition(
                    site_id=report.site_id,
                    edge_id=report.edge_id,
                    occurred_at=report.generated_at,
                    kind="DEVICE_STATE_CHANGED",
                    payload={
                        "previous": "HEALTHY" if alarm.active else "MEASUREMENT_UNAVAILABLE",
                        "current": "MEASUREMENT_UNAVAILABLE" if alarm.active else "HEALTHY",
                        "pitId": pit,
                        "messageId": report.message_id,
                        "scanUpdateAlarm": True,
                        "sensorId": sensor.sensor_id,
                        "alarmState": alarm.state,
                        "reasonCodes": alarm.reason_codes,
                    },
                )
            )
        entries[sensor.sensor_id] = alarm.active
    # Keep at most the configured sensors, including an unresolved unknown episode.
    configured = next(
        (
            set(d.sensor_ids)
            for d in get_settings().EDGE_V2_DEVICES
            if (d.site_id, d.edge_id) == (report.site_id, report.edge_id)
        ),
        set(),
    )
    payload = {"scope": scope, "entries": {k: v for k, v in entries.items() if k in configured}}
    if row is None:
        row = EdgeV2Latest(
            site_id=report.site_id,
            edge_id=report.edge_id,
            kind=key[2],
            message_id=report.message_id,
            generated_at=report.generated_at,
            payload=payload,
        )
        db.add(row)
    else:
        row.payload = payload
        row.message_id = report.message_id
        row.generated_at = report.generated_at


async def unresolved_alarms(db: AsyncSession, report: StatusReport | None) -> set[str]:
    if report is None:
        return set()
    row = await db.get(EdgeV2Latest, (report.site_id, report.edge_id, "scan_update_alarms"))
    if (
        row is None
        or row.message_id != report.message_id
        or row.payload.get("scope") != [report.orchestrator_instance_id, report.config_revision]
    ):
        return set()
    return {sid for sid, active in row.payload.get("entries", {}).items() if active is True}
