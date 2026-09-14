"""Scrap collection (haul) confirmation and history endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.models.config import PitConfig
from src.app.models.log import CollectionLog
from src.app.schemas.log import CollectionConfirmRequest, CollectionLogResponse
from src.app.schemas.state import RealtimeStatePayload
from src.app.services.broadcaster import broadcaster
from src.app.services.state_engine import state_engine

router = APIRouter(prefix="/collections", tags=["Collections"])


@router.post(
    "/confirm",
    response_model=CollectionLogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm scrap collection completion and reset pit status",
)
async def confirm_collection(
    payload: CollectionConfirmRequest,
    db: AsyncSession = Depends(get_db_session),
) -> CollectionLogResponse:
    """Record collection completion by field worker and reset pit alert status."""
    now = datetime.now(UTC)
    settings = get_settings()

    # 1. Fetch pit depth for recalculating residual fill ratio
    stmt = select(PitConfig).where(PitConfig.id == payload.pit_id)
    result = await db.execute(stmt)
    pit_cfg = result.scalar_one_or_none()

    total_depth = pit_cfg.total_depth_cm if pit_cfg else 300.0
    residual_ratio = min(100.0, max(0.0, (payload.residual_height_cm / total_depth) * 100.0))

    # 2. Persist collection log
    collection_log = CollectionLog(
        pit_id=payload.pit_id,
        completed_at=now,
        residual_height_cm=payload.residual_height_cm,
        operator_name=payload.operator_name,
        notes=payload.notes,
    )
    db.add(collection_log)
    await db.commit()
    await db.refresh(collection_log)

    # 3. Explicitly reset state engine for this pit
    state_engine.reset_pit_state(payload.pit_id, residual_fill_ratio=residual_ratio)

    # 4. Broadcast updated state to all connected dashboard clients
    warn_th = pit_cfg.warning_threshold_percent if pit_cfg else settings.DEFAULT_WARNING_THRESHOLD
    crit_th = pit_cfg.critical_threshold_percent if pit_cfg else settings.DEFAULT_CRITICAL_THRESHOLD

    broadcast_payload = RealtimeStatePayload(
        pit_id=payload.pit_id,
        fill_ratio_percent=residual_ratio,
        calculated_height_cm=payload.residual_height_cm,
        state="NORMAL",
        measured_at=now,
        warning_threshold_percent=warn_th,
        critical_threshold_percent=crit_th,
        sensor1_status="OK",
        sensor2_status="OK",
        is_valid=True,
    )
    await broadcaster.broadcast(broadcast_payload)

    return CollectionLogResponse.model_validate(collection_log)


@router.get(
    "/history",
    response_model=list[CollectionLogResponse],
    summary="Query scrap collection history",
)
async def get_collection_history(
    pit_id: str = Query(default="pit-01"),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db_session),
) -> list[CollectionLogResponse]:
    """List historical scrap collection events."""
    stmt = (
        select(CollectionLog)
        .where(CollectionLog.pit_id == pit_id)
        .order_by(desc(CollectionLog.completed_at))
        .limit(limit)
    )
    result = await db.execute(stmt)
    logs = result.scalars().all()
    return [CollectionLogResponse.model_validate(log) for log in logs]
