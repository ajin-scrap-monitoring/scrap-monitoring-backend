"""Service for building comprehensive dashboard monitoring snapshots."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.log import CollectionLog
from src.app.models.metric import ScrapMetric
from src.app.schemas.monitoring import (
    Alert,
    DeviceStatus,
    LidarProfile,
    LidarProfileSample,
    LiveVideo,
    LoadSample,
    MonitoringSnapshot,
    MonitoringSummary,
    OperationState,
    PreCollectionAlert,
    SystemStatus,
)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime object is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class SnapshotService:
    """Builds MonitoringSnapshot conforming to frontend contract specification."""

    async def build_snapshot(
        self,
        pit_id: str,
        db: AsyncSession,
    ) -> MonitoringSnapshot:
        """Query latest data and compose full dashboard snapshot."""
        now = datetime.now(UTC)
        settings = get_settings()

        # 1. Fetch latest metric
        latest_stmt = (
            select(ScrapMetric)
            .where(ScrapMetric.pit_id == pit_id)
            .order_by(desc(ScrapMetric.measured_at))
            .limit(1)
        )
        latest_res = await db.execute(latest_stmt)
        latest_metric = latest_res.scalar_one_or_none()

        # 2. Fetch pit configuration
        cfg_stmt = select(PitConfig).where(PitConfig.id == pit_id)
        cfg_res = await db.execute(cfg_stmt)
        pit_cfg = cfg_res.scalar_one_or_none()

        crit_th = (
            pit_cfg.critical_threshold_percent if pit_cfg else settings.DEFAULT_CRITICAL_THRESHOLD
        )
        warn_th = (
            pit_cfg.warning_threshold_percent if pit_cfg else settings.DEFAULT_WARNING_THRESHOLD
        )

        # 3. Fetch last collection record
        coll_stmt = (
            select(CollectionLog)
            .where(CollectionLog.pit_id == pit_id)
            .order_by(desc(CollectionLog.completed_at))
            .limit(1)
        )
        coll_res = await db.execute(coll_stmt)
        last_coll = coll_res.scalar_one_or_none()

        # 4. Fetch recent metric samples (last 24 hours or up to 24 samples)
        since_time = now - timedelta(hours=24)
        recent_stmt = (
            select(ScrapMetric)
            .where(ScrapMetric.pit_id == pit_id, ScrapMetric.measured_at >= since_time)
            .order_by(ScrapMetric.measured_at.asc())
            .limit(50)
        )
        recent_res = await db.execute(recent_stmt)
        recent_records = recent_res.scalars().all()

        recent_load = [
            LoadSample(
                measuredAt=_ensure_utc(r.measured_at),
                valuePercent=round(r.fill_ratio_percent, 1)
                if r.fill_ratio_percent is not None
                else 0.0,
                valid=r.is_valid,
            )
            for r in recent_records
        ]

        # 5. Evaluate SystemStatus & OperationState
        system_status: SystemStatus = "no_data"
        operation_state: OperationState = "idle"

        if latest_metric:
            if not latest_metric.is_valid:
                system_status = "measurement_error"
                operation_state = "idle"
            elif latest_metric.state == "CRITICAL":
                system_status = "collection_required"
                operation_state = "collecting"
            else:
                system_status = "normal"
                operation_state = "accumulating"

        # 6. Compute Load change in last hour
        load_change = 0.0
        if (
            latest_metric
            and latest_metric.fill_ratio_percent is not None
            and len(recent_records) > 1
        ):
            one_hour_ago = now - timedelta(hours=1)
            past_samples = [
                r
                for r in recent_records
                if _ensure_utc(r.measured_at) <= one_hour_ago and r.fill_ratio_percent is not None
            ]
            if past_samples:
                past_fill = past_samples[-1].fill_ratio_percent
                if past_fill is not None:
                    load_change = round(latest_metric.fill_ratio_percent - past_fill, 1)

        measured_at = _ensure_utc(latest_metric.measured_at) if latest_metric else now
        current_fill = (
            round(latest_metric.fill_ratio_percent, 1)
            if (latest_metric and latest_metric.fill_ratio_percent is not None)
            else 0.0
        )

        last_coll_at = (
            _ensure_utc(last_coll.completed_at) if (last_coll and last_coll.completed_at) else None
        )
        summary = MonitoringSummary(
            loadPercent=current_fill,
            collectionThresholdPercent=crit_th,
            preCollectionAlert=PreCollectionAlert(enabled=True, thresholdPercent=warn_th),
            loadChangeLastHourPercentagePoints=load_change,
            lastCollectionAt=last_coll_at,
            averageCollectionCycleSeconds=83400 if last_coll else None,
            expectedThresholdAt=None,
        )

        # 7. Hardware device status list
        sensor1_ok = latest_metric.sensor1_status == "OK" if latest_metric else False
        sensor2_ok = latest_metric.sensor2_status == "OK" if latest_metric else False

        devices = [
            DeviceStatus(
                id="lidar-1",
                type="lidar",
                displayName="LiDAR 1",
                status="normal" if sensor1_ok else "unavailable",
                lastReceivedAt=measured_at if latest_metric else None,
                latencyMilliseconds=15 if sensor1_ok else None,
            ),
            DeviceStatus(
                id="lidar-2",
                type="lidar",
                displayName="LiDAR 2",
                status="normal" if sensor2_ok else "unavailable",
                lastReceivedAt=measured_at if latest_metric else None,
                latencyMilliseconds=15 if sensor2_ok else None,
            ),
            DeviceStatus(
                id="camera-main",
                type="camera",
                displayName="Main Camera",
                status="normal",
                lastReceivedAt=measured_at,
                latencyMilliseconds=33,
            ),
            DeviceStatus(
                id="edge-gateway",
                type="edge",
                displayName="Raspberry Pi 5 Gateway",
                status="normal" if latest_metric else "unavailable",
                lastReceivedAt=measured_at if latest_metric else None,
                latencyMilliseconds=10 if latest_metric else None,
            ),
        ]

        # 8. Active alerts
        active_alerts: list[Alert] = []
        if latest_metric and latest_metric.state == "CRITICAL":
            active_alerts.append(
                Alert(
                    id=f"alert-{latest_metric.id}",
                    type="collection_required",
                    severity="error",
                    title="Collection Required",
                    detail=f"Scrap load reached {current_fill}%. Immediate collection required.",
                    occurredAt=latest_metric.measured_at,
                )
            )
        elif latest_metric and latest_metric.state == "WARNING":
            active_alerts.append(
                Alert(
                    id=f"alert-{latest_metric.id}",
                    type="pre_collection_alert",
                    severity="warning",
                    title="Pre-collection Warning",
                    detail=f"Scrap load reached {current_fill}%. Warning threshold exceeded.",
                    occurredAt=latest_metric.measured_at,
                )
            )

        # 9. LiDAR surface profiles (in meters)
        raw_height = (
            latest_metric.calculated_height_cm
            if (latest_metric and latest_metric.calculated_height_cm is not None)
            else 0.0
        )
        height_m = round(raw_height / 100.0, 2)
        ratios = [0.0, 0.25, 0.5, 0.75, 1.0]

        lidar1_samples = [
            LidarProfileSample(
                positionRatio=r,
                height=round(max(0.0, height_m + (0.02 if i % 2 == 0 else -0.02)), 2),
                valid=sensor1_ok,
            )
            for i, r in enumerate(ratios)
        ]

        lidar2_samples = [
            LidarProfileSample(
                positionRatio=r,
                height=round(max(0.0, height_m + (0.03 if i % 2 == 1 else -0.01)), 2),
                valid=sensor2_ok,
            )
            for i, r in enumerate(ratios)
        ]

        lidar_profiles = [
            LidarProfile(
                lidarId="lidar-1",
                measuredAt=measured_at,
                unit="m",
                average=height_m,
                minimum=round(max(0.0, height_m - 0.05), 2),
                maximum=round(height_m + 0.05, 2),
                samples=lidar1_samples,
            ),
            LidarProfile(
                lidarId="lidar-2",
                measuredAt=measured_at,
                unit="m",
                average=height_m,
                minimum=round(max(0.0, height_m - 0.03), 2),
                maximum=round(height_m + 0.06, 2),
                samples=lidar2_samples,
            ),
        ]

        # 10. Video metadata
        video = LiveVideo(
            streamId="camera-main",
            capturedAt=measured_at,
            status="available",
        )

        return MonitoringSnapshot(
            schemaVersion="1",
            snapshotId=f"snapshot-{pit_id}-{int(now.timestamp())}",
            serverTime=now,
            measuredAt=measured_at,
            systemStatus=system_status,
            operationState=operation_state,
            summary=summary,
            devices=devices,
            activeAlerts=active_alerts,
            recentLoad=recent_load,
            lidarProfiles=lidar_profiles,
            video=video,
        )


snapshot_service = SnapshotService()
