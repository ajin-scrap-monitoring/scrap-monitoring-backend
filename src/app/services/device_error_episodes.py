"""Group repeated legacy device causes; preserve raw evidence and confirmed sensor alarms."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.edge_v2 import StatusReport
from src.app.services.scan_update_alarm_service import managed_report

MAX_CAUSES = 128


async def mark_device_episode(
    db: AsyncSession, report: StatusReport, transition: EdgeV2Transition | None
) -> None:
    """Called under the existing device projection lock, inside the receipt transaction.

    Legacy overall reports cannot confirm recovery of each individual cause. Remember
    publicly recorded causes until a healthy, reason-free report, or a scope change.
    A new cause remains visible. Never rewrite past transitions or current health.
    """
    key = (report.site_id, report.edge_id, "device_error_episodes")
    row = await db.get(EdgeV2Latest, key)
    pit = next(
        (
            s.pit_id
            for s in get_settings().EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (report.site_id, report.edge_id)
        ),
        None,
    )
    scope = [report.orchestrator_instance_id, report.config_revision, pit]
    # Confirmed per-sensor alarms already have independent active/recovery episodes.
    # A switch to that evidence must not leave stale legacy suppression behind.
    confirmed = managed_report(report)
    clear = report.overall_state == "HEALTHY" and not report.reason_codes
    previous = row.payload if row is not None and row.payload.get("scope") == scope else {}
    values = previous.get("causes", [])
    recorded = (
        set(values)
        if isinstance(values, list)
        and len(values) <= MAX_CAUSES
        and all(isinstance(x, str) for x in values)
        else set()
    )
    if confirmed or clear:
        recorded = set()
    elif transition is not None and transition.payload.get("diagnosticOnly") is not True:
        causes = {"reason:" + value for value in report.reason_codes}
        if not causes:
            causes = {"state:" + report.overall_state}
        if report.overall_state == "CONFIG_ERROR":
            causes.add("state:CONFIG_ERROR")
        if len(recorded | causes) > MAX_CAUSES:
            # Insufficient bounded evidence cannot justify hiding an operational event.
            recorded = set()
        else:
            if causes <= recorded:
                transition.payload = {
                    **transition.payload,
                    "diagnosticOnly": True,
                    "deviceEpisodeContinuation": True,
                }
            recorded |= causes
    if row is None:
        if not recorded:
            return
        row = EdgeV2Latest(
            site_id=report.site_id,
            edge_id=report.edge_id,
            kind=key[2],
            message_id=report.message_id,
            generated_at=report.generated_at,
            payload={"scope": scope, "causes": sorted(recorded)},
        )
        db.add(row)
    else:
        row.payload = {"scope": scope, "causes": sorted(recorded)}
        row.message_id = report.message_id
        row.generated_at = report.generated_at
