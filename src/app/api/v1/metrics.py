"""Metric ingestion and historical query endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.security import verify_edge_api_key
from src.app.db.session import get_db_session
from src.app.models.config import PitConfig
from src.app.models.metric import ScrapMetric
from src.app.schemas.metric import MetricIngestRequest, MetricResponse

router = APIRouter(prefix="/metrics", tags=["Metrics"])


@router.post(
    "/ingest",
    response_model=MetricResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest LiDAR sensor metrics",
)
async def ingest_metric(
    payload: MetricIngestRequest,
    db: AsyncSession = Depends(get_db_session),
    _edge_auth: str = Depends(verify_edge_api_key),
) -> MetricResponse:
    """Receive processed scrap level metrics from Raspberry Pi Edge."""
    settings = get_settings()

    # 1. Fetch pit configuration for custom thresholds if available
    stmt = select(PitConfig).where(PitConfig.id == payload.pit_id)
    result = await db.execute(stmt)
    pit_cfg = result.scalar_one_or_none()

    warn_th = pit_cfg.warning_threshold_percent if pit_cfg else settings.DEFAULT_WARNING_THRESHOLD
    crit_th = pit_cfg.critical_threshold_percent if pit_cfg else settings.DEFAULT_CRITICAL_THRESHOLD

    # 2. Evaluate state
    state = "NORMAL"
    if payload.fill_ratio_percent >= crit_th:
        state = "CRITICAL"
    elif payload.fill_ratio_percent >= warn_th:
        state = "WARNING"

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

    return MetricResponse.model_validate(metric_record)


@router.get(
    "/latest",
    response_model=MetricResponse | None,
    summary="Get latest scrap metric snapshot",
)
async def get_latest_metric(
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> MetricResponse | None:
    """Retrieve the most recent metric entry for a given pit."""
    stmt = (
        select(ScrapMetric)
        .where(ScrapMetric.pit_id == pit_id)
        .order_by(desc(ScrapMetric.measured_at))
        .limit(1)
    )
    result = await db.execute(stmt)
    metric = result.scalar_one_or_none()
    if not metric:
        return None
    return MetricResponse.model_validate(metric)


@router.get(
    "/history",
    response_model=list[MetricResponse],
    summary="Query time-series metric history",
)
async def get_metric_history(
    pit_id: str = Query(default="pit-01"),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db_session),
) -> list[MetricResponse]:
    """Query time-series metrics with optional time range filtering."""
    stmt = select(ScrapMetric).where(ScrapMetric.pit_id == pit_id)

    if start_time:
        stmt = stmt.where(ScrapMetric.measured_at >= start_time)
    if end_time:
        stmt = stmt.where(ScrapMetric.measured_at <= end_time)

    stmt = stmt.order_by(desc(ScrapMetric.measured_at)).limit(limit)
    result = await db.execute(stmt)
    records = result.scalars().all()
    return [MetricResponse.model_validate(record) for record in records]
