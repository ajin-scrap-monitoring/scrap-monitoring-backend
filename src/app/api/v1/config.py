"""Pit configuration and threshold management endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.models.config import PitConfig
from src.app.schemas.config import PitConfigResponse, PitConfigUpdateRequest

router = APIRouter(prefix="/config", tags=["Configuration"])


@router.get("/pits", response_model=list[PitConfigResponse], summary="List all pit configurations")
async def list_pit_configs(db: AsyncSession = Depends(get_db_session)) -> list[PitConfigResponse]:
    """List all registered scrap pit configurations."""
    stmt = select(PitConfig).order_by(PitConfig.id)
    result = await db.execute(stmt)
    pits = result.scalars().all()

    # If empty, return default pit
    if not pits:
        settings = get_settings()
        default_pit = PitConfig(
            id="pit-01",
            name="1호 라인 스크랩 구덩이",
            total_depth_cm=300.0,
            warning_threshold_percent=settings.DEFAULT_WARNING_THRESHOLD,
            critical_threshold_percent=settings.DEFAULT_CRITICAL_THRESHOLD,
        )
        db.add(default_pit)
        await db.commit()
        await db.refresh(default_pit)
        pits = [default_pit]

    return [PitConfigResponse.model_validate(p) for p in pits]


@router.get("/pits/{pit_id}", response_model=PitConfigResponse, summary="Get pit configuration")
async def get_pit_config(
    pit_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> PitConfigResponse:
    """Get configuration for a specific pit."""
    stmt = select(PitConfig).where(PitConfig.id == pit_id)
    result = await db.execute(stmt)
    pit = result.scalar_one_or_none()

    if not pit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pit with ID '{pit_id}' not found",
        )
    return PitConfigResponse.model_validate(pit)


@router.put("/pits/{pit_id}", response_model=PitConfigResponse, summary="Update pit configuration")
async def update_pit_config(
    pit_id: str,
    payload: PitConfigUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> PitConfigResponse:
    """Update threshold limits or physical depth of a scrap pit."""
    stmt = select(PitConfig).where(PitConfig.id == pit_id)
    result = await db.execute(stmt)
    pit = result.scalar_one_or_none()

    if not pit:
        # Create new configuration entry
        settings = get_settings()
        pit = PitConfig(
            id=pit_id,
            name=payload.name or f"구덩이 {pit_id}",
            total_depth_cm=payload.total_depth_cm or 300.0,
            warning_threshold_percent=(
                payload.warning_threshold_percent
                if payload.warning_threshold_percent is not None
                else settings.DEFAULT_WARNING_THRESHOLD
            ),
            critical_threshold_percent=(
                payload.critical_threshold_percent
                if payload.critical_threshold_percent is not None
                else settings.DEFAULT_CRITICAL_THRESHOLD
            ),
        )
        db.add(pit)
    else:
        if payload.name is not None:
            pit.name = payload.name
        if payload.total_depth_cm is not None:
            pit.total_depth_cm = payload.total_depth_cm
        if payload.warning_threshold_percent is not None:
            pit.warning_threshold_percent = payload.warning_threshold_percent
        if payload.critical_threshold_percent is not None:
            pit.critical_threshold_percent = payload.critical_threshold_percent

    await db.commit()
    await db.refresh(pit)
    return PitConfigResponse.model_validate(pit)
