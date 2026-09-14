"""Metric Service orchestrating ingestion, state evaluation, broadcast, and persistence."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.metric import ScrapMetric
from src.app.schemas.metric import MetricIngestRequest, MetricResponse
from src.app.schemas.state import RealtimeStatePayload
from src.app.services.alert_engine import alert_engine
from src.app.services.broadcaster import broadcaster
from src.app.services.state_engine import state_engine

logger = logging.getLogger(__name__)


class MetricService:
    """Orchestrates metric ingestion pipeline."""

    async def ingest_metric(
        self,
        payload: MetricIngestRequest,
        db: AsyncSession,
    ) -> MetricResponse:
        """Process incoming LiDAR metric: evaluate, persist, broadcast, and trigger alert."""
        settings = get_settings()

        # 1. Fetch pit configuration for custom thresholds if available
        stmt = select(PitConfig).where(PitConfig.id == payload.pit_id)
        result = await db.execute(stmt)
        pit_cfg = result.scalar_one_or_none()

        warn_th = (
            pit_cfg.warning_threshold_percent if pit_cfg else settings.DEFAULT_WARNING_THRESHOLD
        )
        crit_th = (
            pit_cfg.critical_threshold_percent if pit_cfg else settings.DEFAULT_CRITICAL_THRESHOLD
        )

        # 2. Evaluate State with Hysteresis
        state, is_new_critical = state_engine.evaluate_state(
            pit_id=payload.pit_id,
            fill_ratio_percent=payload.fill_ratio_percent,
            warning_threshold=warn_th,
            critical_threshold=crit_th,
        )

        # 3. Sensor diagnostic quality check
        is_valid = True
        if payload.sensor1_status != "OK" and payload.sensor2_status != "OK":
            is_valid = False

        # 4. Persist ScrapMetric in Database
        metric_record = ScrapMetric(
            pit_id=payload.pit_id,
            measured_at=payload.measured_at,
            lidar1_distance_cm=payload.lidar1_distance_cm,
            lidar2_distance_cm=payload.lidar2_distance_cm,
            calculated_height_cm=payload.calculated_height_cm,
            fill_ratio_percent=payload.fill_ratio_percent,
            state=state,
            sensor1_status=payload.sensor1_status,
            sensor2_status=payload.sensor2_status,
            is_valid=is_valid,
        )
        db.add(metric_record)
        await db.flush()
        await db.refresh(metric_record)

        # 5. Broadcast to real-time clients (WebSocket / SSE)
        realtime_payload = RealtimeStatePayload(
            pit_id=payload.pit_id,
            fill_ratio_percent=payload.fill_ratio_percent,
            calculated_height_cm=payload.calculated_height_cm,
            state=state,
            measured_at=payload.measured_at,
            warning_threshold_percent=warn_th,
            critical_threshold_percent=crit_th,
            sensor1_status=payload.sensor1_status,
            sensor2_status=payload.sensor2_status,
            is_valid=is_valid,
        )
        await broadcaster.broadcast(realtime_payload)

        # 6. Trigger alert engine if critical state
        if state == "CRITICAL":
            await alert_engine.handle_alert(
                pit_id=payload.pit_id,
                fill_ratio_percent=payload.fill_ratio_percent,
                is_new_candidate=is_new_critical,
            )

        return MetricResponse.model_validate(metric_record)


metric_service = MetricService()
