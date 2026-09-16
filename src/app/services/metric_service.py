"""Metric Service orchestrating ingestion, state evaluation, broadcast, and persistence."""

import hashlib
import json
import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.metric import ScrapMetric
from src.app.schemas.edge import EdgeMetricIngestRequest, EdgeMetricIngestResponse
from src.app.schemas.metric import MetricIngestRequest, MetricResponse
from src.app.schemas.state import RealtimeStatePayload
from src.app.services.alert_engine import alert_engine
from src.app.services.broadcaster import broadcaster
from src.app.services.state_engine import state_engine

logger = logging.getLogger(__name__)


class MetricService:
    """Orchestrates metric ingestion pipeline."""

    async def ingest_edge_metric(
        self,
        payload: EdgeMetricIngestRequest,
        db: AsyncSession,
    ) -> EdgeMetricIngestResponse:
        """Ingest Edge Platform contract v1.0 measurement with idempotency guarantee."""
        settings = get_settings()
        pit_id = payload.site_id or "pit-01"

        # Compute payload hash for idempotency conflict check
        payload_json = payload.model_dump_json()
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

        # 1. Idempotency Check: search by measurement_id
        stmt = select(ScrapMetric).where(ScrapMetric.measurement_id == payload.measurement_id)
        res = await db.execute(stmt)
        existing = res.scalar_one_or_none()

        if existing:
            if existing.idempotency_payload_hash == payload_hash:
                logger.info(
                    "Duplicate edge measurement acknowledged: %s",
                    payload.measurement_id,
                )
                return EdgeMetricIngestResponse(
                    measurement_id=payload.measurement_id,
                    accepted=True,
                    duplicate=True,
                )
            msg = (
                f"Conflict: measurement_id '{payload.measurement_id}' exists with different payload"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=msg,
            )

        # 2. Extract fill_percentage & calculate height
        fill_percentage: float | None = payload.fill_percentage
        if fill_percentage is None and payload.fill_ratio is not None:
            fill_percentage = round(payload.fill_ratio * 100.0, 2)

        # Parse sensor details
        s1_dist: float | None = None
        s2_dist: float | None = None
        s1_status = "OK"
        s2_status = "OK"

        for s in payload.sensors:
            h_cm = s.height / 10.0 if (s.height is not None and s.height > 600.0) else s.height
            dist_cm = (300.0 - h_cm) if h_cm is not None else None
            if "1" in s.instance or s.sequence == 1:
                s1_dist = dist_cm
                s1_status = s.status
            elif "2" in s.instance or s.sequence == 2:
                s2_dist = dist_cm
                s2_status = s.status

        # Compute representative height
        calculated_height_cm: float | None = None
        if payload.overall_quality != "INVALID":
            if fill_percentage is not None:
                calculated_height_cm = round(300.0 * (fill_percentage / 100.0), 2)
            elif s1_dist is not None or s2_dist is not None:
                valid_heights = [(300.0 - d) for d in (s1_dist, s2_dist) if d is not None]
                if valid_heights:
                    calculated_height_cm = round(sum(valid_heights) / len(valid_heights), 2)
                    fill_percentage = round((calculated_height_cm / 300.0) * 100.0, 2)

        # 3. Evaluate state with hysteresis (only for valid/degraded measurements)
        is_valid = payload.overall_quality != "INVALID"
        state = "NORMAL"
        is_new_critical = False

        cfg_stmt = select(PitConfig).where(PitConfig.id == pit_id)
        cfg_res = await db.execute(cfg_stmt)
        pit_cfg = cfg_res.scalar_one_or_none()
        warn_th = (
            pit_cfg.warning_threshold_percent if pit_cfg else settings.DEFAULT_WARNING_THRESHOLD
        )
        crit_th = (
            pit_cfg.critical_threshold_percent if pit_cfg else settings.DEFAULT_CRITICAL_THRESHOLD
        )

        if is_valid and fill_percentage is not None:
            state, is_new_critical = state_engine.evaluate_state(
                pit_id=pit_id,
                fill_ratio_percent=fill_percentage,
                warning_threshold=warn_th,
                critical_threshold=crit_th,
            )
        elif not is_valid:
            state = "ERROR"

        # 4. Save ScrapMetric to DB
        sensors_json = (
            json.dumps([s.model_dump() for s in payload.sensors]) if payload.sensors else None
        )

        metric_record = ScrapMetric(
            measurement_id=payload.measurement_id,
            idempotency_payload_hash=payload_hash,
            site_id=payload.site_id,
            edge_id=payload.edge_id,
            pit_id=pit_id,
            measured_at=payload.measured_at,
            overall_quality=payload.overall_quality,
            quality_reason_code=payload.quality_reason_code,
            lidar1_distance_cm=s1_dist,
            lidar2_distance_cm=s2_dist,
            calculated_height_cm=calculated_height_cm,
            fill_ratio_percent=fill_percentage,
            state=state,
            sensor1_status=s1_status,
            sensor2_status=s2_status,
            sensors_data=sensors_json,
            is_valid=is_valid,
        )
        db.add(metric_record)
        await db.commit()

        # 5. Broadcast to real-time clients (SSE/WS)
        realtime_payload = RealtimeStatePayload(
            pit_id=pit_id,
            fill_ratio_percent=fill_percentage or 0.0,
            calculated_height_cm=calculated_height_cm or 0.0,
            state=state,
            measured_at=payload.measured_at,
            warning_threshold_percent=warn_th,
            critical_threshold_percent=crit_th,
            sensor1_status=s1_status,
            sensor2_status=s2_status,
            is_valid=is_valid,
        )
        await broadcaster.broadcast(realtime_payload)

        # 6. Trigger alert if critical
        if state == "CRITICAL" and fill_percentage is not None:
            await alert_engine.handle_alert(
                pit_id=pit_id,
                fill_ratio_percent=fill_percentage,
                is_new_candidate=is_new_critical,
            )

        return EdgeMetricIngestResponse(
            measurement_id=payload.measurement_id,
            accepted=True,
        )

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
