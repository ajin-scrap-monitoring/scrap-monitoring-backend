"""Metric ingestion and historical query endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import verify_edge_api_key
from src.app.db.session import get_db_session
from src.app.models.metric import ScrapMetric
from src.app.schemas.metric import MetricIngestRequest, MetricResponse, MetricStatsSummary
from src.app.services.metric_service import metric_service

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
    return await metric_service.ingest_metric(payload, db)


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


@router.get(
    "/stats",
    response_model=MetricStatsSummary | None,
    summary="Query aggregated metric statistics",
)
async def get_metric_stats(
    pit_id: str = Query(default="pit-01"),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> MetricStatsSummary | None:
    """Query aggregated statistical summary for a given pit and time range."""
    stmt = select(
        func.avg(ScrapMetric.fill_ratio_percent),
        func.max(ScrapMetric.fill_ratio_percent),
        func.min(ScrapMetric.fill_ratio_percent),
        func.avg(ScrapMetric.calculated_height_cm),
        func.count(ScrapMetric.id),
    ).where(ScrapMetric.pit_id == pit_id)

    if start_time:
        stmt = stmt.where(ScrapMetric.measured_at >= start_time)
    if end_time:
        stmt = stmt.where(ScrapMetric.measured_at <= end_time)

    result = await db.execute(stmt)
    avg_fill, max_fill, min_fill, avg_h, count = result.one()
    if not count or count == 0:
        return None

    return MetricStatsSummary(
        timestamp=datetime.now(UTC),
        avg_fill_ratio=round(float(avg_fill), 2),
        max_fill_ratio=round(float(max_fill), 2),
        min_fill_ratio=round(float(min_fill), 2),
        avg_height_cm=round(float(avg_h), 2),
        sample_count=count,
    )
