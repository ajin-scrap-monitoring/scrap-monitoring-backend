"""Read-only v2 dashboard projection; no legacy metrics, alerts or recording jobs."""

from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2Operation
from src.app.models.edge_v2_projection import EdgeV2Filter
from src.app.schemas.edge_v2 import Assessment, Configuration, Measurement
from src.app.schemas.monitoring_v2 import (
    DashboardDataState,
    LidarProfilePageV2,
    LidarProfileV2,
    MonitoringSnapshotV2,
    MonitoringSource,
    MonitoringSummaryV2,
    OperationalState,
    OperationStatus,
)
from src.app.services.edge_v2_projection import latest_message
from src.app.services.edge_v2_service import device_status, digest, utc
from src.app.services.lidar_height import height_profile


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
    if message is not None and message.compact:
        message = await latest_message(db, source.site_id, source.edge_id, "state")
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
    raw_percent = percent
    filter_state: Literal["DISABLED", "WARMING", "READY", "UNAVAILABLE"] = "DISABLED"
    if message is not None and message.compact:
        filtered = await db.get(EdgeV2Filter, (source.site_id, source.edge_id))
        filter_state = "UNAVAILABLE"
        if (
            state == "FRESH"
            and filtered
            and filtered.payload.get("message_id") == message.message_id
        ):
            value = filtered.payload.get("value")
            if filtered.payload.get("state") == "READY" and value is not None:
                percent = value
                filter_state = "READY"
            else:
                percent = None
                state = "UNKNOWN"
                filter_state = (
                    "WARMING" if filtered.payload.get("state") == "WARMING" else "UNAVAILABLE"
                )
                reasons.append("FILTER_" + filter_state)
        else:
            percent = None
            if state == "FRESH":
                state = "UNKNOWN"
                reasons.append("FILTER_UNAVAILABLE")
    operations: OperationStatus | None = None
    if settings.EDGE_V2_OPERATIONS_ENABLED:
        row = await db.get(EdgeV2Operation, source.pit_id)
        if row and (row.site_id, row.edge_id) != (source.site_id, source.edge_id):
            row = None
        evaluated = (
            row is not None and assessment is not None and row.message_id == assessment.message_id
        )
        effective = (
            row.state
            if evaluated and row and state == "FRESH"
            else ("ERROR" if state == "UNAVAILABLE" else "UNKNOWN")
        )
        operations = OperationStatus(
            lastKnownState=cast(OperationalState, row.state) if row else None,
            effectiveState=cast(OperationalState | Literal["ERROR", "UNKNOWN"], effective),
            lastEvaluatedAt=utc(row.observed_at) if row else None,
            lastAlertAt=utc(row.last_alert_at) if row and row.last_alert_at else None,
            notificationId=f"notif-{row.last_alert_id}" if row and row.last_alert_id else None,
        )
    critical_threshold = (
        config.critical_threshold_percent if config else settings.EDGE_V2_CRITICAL_THRESHOLD
    )
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
            rawLoadPercent=raw_percent,
            filterState=filter_state,
            warningThresholdPercent=config.warning_threshold_percent
            if config
            else settings.DEFAULT_WARNING_THRESHOLD,
            collectionThresholdPercent=critical_threshold,
            recoveryThresholdPercent=max(0.0, critical_threshold - 5.0),
        ),
        assessment=assessment,
        deviceStatus=device,
        operations=operations,
    )
    # Stable identity suppresses unchanged SSE frames while timeout transitions remain visible.
    snapshot.snapshotId = digest(
        snapshot.model_dump(mode="json", exclude={"serverTime", "snapshotId"})
    )
    return snapshot


async def lidar_profiles(
    db: AsyncSession, source: MonitoringSource, now: datetime | None = None
) -> LidarProfilePageV2:
    settings = get_settings()
    device = next(
        d
        for d in settings.EDGE_V2_DEVICES
        if (d.site_id, d.edge_id) == (source.site_id, source.edge_id)
    )
    message = await latest_message(db, source.site_id, source.edge_id, "measurement")
    reference = next(
        (r for r in settings.EDGE_V2_HEIGHT_REFERENCES if r.pit_id == source.pit_id), None
    )
    if reference and message:
        observation = Measurement.model_validate(message.payload)
        registration = await db.scalar(
            select(EdgeV2Message)
            .where(
                EdgeV2Message.site_id == source.site_id,
                EdgeV2Message.edge_id == source.edge_id,
                EdgeV2Message.message_type == "edge_configuration",
                EdgeV2Message.config_revision == observation.config_revision,
            )
            .limit(1)
        )
        configuration = Configuration.model_validate(registration.payload) if registration else None
        pit = await db.get(PitConfig, source.pit_id)
        when = now or datetime.now(UTC)
        status = await device_status(db, source.site_id, source.edge_id, when)
        profiles = [
            height_profile(
                observation,
                sensor,
                configuration,
                reference,
                pit.total_depth_cm if pit else None,
                when,
                settings.EDGE_V2_ASSESSMENT_TTL_SECONDS,
                settings.EDGE_V2_EMPTY_FLOOR_DISTANCE_CM,
            )
            for sensor in observation.sensors
        ]
        # A healthy process does not prove either sensor is fresh and using this configuration.
        for profile in profiles:
            sensor_status = (
                next((s for s in status.snapshot.sensors if s.sensor_id == profile.sensorId), None)
                if status.snapshot
                else None
            )
            if (
                status.effective_state not in ("HEALTHY", "DEGRADED")
                or status.snapshot is None
                or status.snapshot.config_revision != observation.config_revision
                or status.snapshot.clock.state != "SYNCED"
                or status.snapshot.generated_at > when
                or sensor_status is None
                or sensor_status.data_state != "FRESH"
                or sensor_status.last_scan_at is None
                or sensor_status.last_scan_at > when
                or when - sensor_status.last_scan_at
                >= timedelta(seconds=settings.EDGE_V2_ASSESSMENT_TTL_SECONDS)
            ):
                profile.calibrationState = "UNAVAILABLE"
                profile.average = profile.minimum = profile.maximum = None
                profile.sectionFillPercent = None
                profile.samples = []
                profile.coverageRatio = 0
                profile.reasonCodes = ["HEIGHT_DEVICE_OR_SENSOR_UNAVAILABLE"]
        page = LidarProfilePageV2(pitId=source.pit_id, profiles=profiles)
        values = [p.sectionFillPercent for p in profiles if p.sectionFillPercent is not None]
        if observation.pair_state != "MATCHED":
            page.meanSectionReasonCodes = ["HEIGHT_PAIR_UNMATCHED"]
        elif len(values) != 2:
            page.meanSectionReasonCodes = ["HEIGHT_SECTION_UNAVAILABLE"]
        else:
            page.meanSectionFillPercent = fmean(values)
        return page
    sensors = {s["sensor_id"]: s for s in message.payload["sensors"]} if message else {}
    profiles = []
    for sensor_id in device.sensor_ids:
        sensor = sensors.get(sensor_id, {})
        scan = sensor.get("scan")
        profiles.append(
            LidarProfileV2(
                sensorId=sensor_id,
                measuredAt=datetime.fromisoformat(scan["acquired_at"]) if scan else None,
                configRevision=message.config_revision if message else None,
                calibrationVersion=sensor.get("calibration_version"),
                referenceDistanceCm=settings.EDGE_V2_EMPTY_FLOOR_DISTANCE_CM,
            )
        )
    return LidarProfilePageV2(pitId=source.pit_id, profiles=profiles)
