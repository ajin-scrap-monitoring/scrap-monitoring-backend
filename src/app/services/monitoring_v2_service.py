"""Read-only v2 dashboard projection; no legacy metrics, alerts or recording jobs."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.schemas.edge_v2 import Assessment
from src.app.schemas.monitoring_v2 import (
    DashboardDataState,
    MonitoringSnapshotV2,
    MonitoringSource,
    MonitoringSummaryV2,
)
from src.app.services.edge_v2_service import device_status, digest, utc


def monitoring_source(pit_id: str) -> MonitoringSource:
    for source in get_settings().EDGE_V2_MONITORING_SOURCES:
        if source.pit_id == pit_id:
            return source
    raise HTTPException(404, "MONITORING_SOURCE_NOT_CONFIGURED")


async def build_snapshot(
    db: AsyncSession, source: MonitoringSource, now: datetime | None = None
) -> MonitoringSnapshotV2:
    """Newest generated STATE wins; receipt/replay and EVENT revisions cannot replace it."""
    now = now or datetime.now(UTC)
    settings = get_settings()
    message = await db.scalar(
        select(EdgeV2Message)
        .where(
            EdgeV2Message.site_id == source.site_id,
            EdgeV2Message.edge_id == source.edge_id,
            EdgeV2Message.message_type == "edge_assessment",
            EdgeV2Message.payload["assessment_type"].as_string() == "STATE",
        )
        .order_by(EdgeV2Message.generated_at.desc(), EdgeV2Message.id.asc())
        .limit(1)
    )
    assessment = Assessment.model_validate(message.payload) if message else None
    device = await device_status(db, source.site_id, source.edge_id, now)
    config = await db.get(PitConfig, source.pit_id)
    measured_at = assessment.observed_to if assessment else None
    reasons: list[str] = []
    state: DashboardDataState = "NO_DATA"
    percent: float | None = None
    if assessment:
        reasons.extend(assessment.reason_codes)
        freshness_time = measured_at or assessment.generated_at
        if assessment.generated_at > now + timedelta(seconds=5) or freshness_time > now:
            state = "UNKNOWN"
            reasons.append("ASSESSMENT_CLOCK_SKEW")
        elif now - freshness_time >= timedelta(seconds=settings.EDGE_V2_ASSESSMENT_TTL_SECONDS):
            state = "STALE"
            reasons.append("ASSESSMENT_STALE")
        elif device.connection_state != "REPORTING":
            state = "UNKNOWN"
            reasons.append("STATUS_NOT_RECEIVED")
        elif device.effective_state in ("MEASUREMENT_UNAVAILABLE", "CONFIG_ERROR", "UNKNOWN"):
            state = "UNAVAILABLE"
            reasons.append("DEVICE_MEASUREMENT_UNAVAILABLE")
        elif device.snapshot and device.snapshot.config_revision != assessment.config_revision:
            state = "UNKNOWN"
            reasons.append("CONFIG_REVISION_MISMATCH")
        elif assessment.fill_estimate is None or assessment.decision_state == "UNAVAILABLE":
            state = "UNAVAILABLE"
        elif measured_at is None:
            state = "UNKNOWN"
            reasons.append("OBSERVATION_TIME_UNAVAILABLE")
        else:
            # The receiver validates capability/rule prerequisites. Do not infer fill from scans.
            state = "FRESH"
            percent = round(assessment.fill_estimate.ratio * 100, 6)
    else:
        reasons.append("ASSESSMENT_NOT_RECEIVED")
    snapshot = MonitoringSnapshotV2(
        snapshotId="",
        serverTime=now,
        pitId=source.pit_id,
        source=source,
        measuredAt=measured_at,
        assessmentReceivedAt=utc(message.received_at) if message else None,
        dataState=state,
        reasonCodes=list(dict.fromkeys(reasons)),
        summary=MonitoringSummaryV2(
            loadPercent=percent,
            warningThresholdPercent=config.warning_threshold_percent
            if config
            else settings.DEFAULT_WARNING_THRESHOLD,
            collectionThresholdPercent=config.critical_threshold_percent
            if config
            else settings.DEFAULT_CRITICAL_THRESHOLD,
        ),
        assessment=assessment,
        deviceStatus=device,
    )
    # Stable identity suppresses unchanged SSE frames while timeout transitions remain visible.
    snapshot.snapshotId = digest(
        snapshot.model_dump(mode="json", exclude={"serverTime", "snapshotId"})
    )
    return snapshot
