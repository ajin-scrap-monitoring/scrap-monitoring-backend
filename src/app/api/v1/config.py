"""Pit configuration and threshold management endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.admin_access import private_response, require_admin_change
from src.app.api.errors import error_responses
from src.app.api.v1.session import get_session
from src.app.core.alert_thresholds import validate_alert_thresholds
from src.app.core.config import get_settings
from src.app.db.base import utc_now
from src.app.db.session import get_db_session
from src.app.models.config import PitConfig
from src.app.schemas.config import PitConfigResponse, PitConfigUpdateRequest
from src.app.schemas.session import Session
from src.app.services.settings_history_service import pit_values, record_change

router = APIRouter(
    prefix="/config",
    tags=["Configuration"],
    dependencies=[Depends(get_session), Depends(private_response)],
    responses=error_responses(401, 422, 503),
)


PitId = Annotated[str, Path(min_length=1, max_length=50)]


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


@router.get(
    "/pits/{pit_id}",
    response_model=PitConfigResponse,
    summary="Get pit configuration",
    responses=error_responses(404),
)
async def get_pit_config(
    pit_id: PitId,
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


@router.put(
    "/pits/{pit_id}",
    response_model=PitConfigResponse,
    summary="Update pit configuration",
    dependencies=[Depends(require_admin_change)],
    responses=error_responses(403, 409),
)
async def update_pit_config(
    pit_id: PitId,
    payload: PitConfigUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
    session: Session = Depends(require_admin_change),
) -> PitConfigResponse:
    """Update threshold limits or physical depth of a scrap pit."""
    stmt = (
        select(PitConfig)
        .where(PitConfig.id == pit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    pit = result.scalar_one_or_none()
    before = pit_values(pit) if pit else None

    settings = get_settings()
    warning = payload.warning_threshold_percent
    if warning is None:
        warning = pit.warning_threshold_percent if pit else settings.DEFAULT_WARNING_THRESHOLD
    critical = payload.critical_threshold_percent
    if critical is None:
        critical = pit.critical_threshold_percent if pit else settings.DEFAULT_CRITICAL_THRESHOLD
    # Validate the combined values before changing an existing row or creating one.
    try:
        validate_alert_thresholds(warning, critical)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None

    if not pit:
        # Create new configuration entry
        pit = PitConfig(
            id=pit_id,
            name=payload.name or f"구덩이 {pit_id}",
            total_depth_cm=payload.total_depth_cm or 300.0,
            warning_threshold_percent=warning,
            critical_threshold_percent=critical,
        )
        db.add(pit)
    else:
        assert before is not None
        values = {**before, **payload.model_dump(exclude_none=True)}
        # SQLite ignores FOR UPDATE; compare every previous business value so
        # the history cannot claim a predecessor that another writer replaced.
        statement = (
            update(PitConfig)
            .where(
                PitConfig.id == pit_id,
                PitConfig.updated_at == pit.updated_at,
                *(getattr(PitConfig, key) == value for key, value in before.items()),
            )
            .values(**values, updated_at=utc_now() if values != before else pit.updated_at)
        )
        try:
            changed = await db.execute(statement.execution_options(synchronize_session=False))
            if getattr(changed, "rowcount", 0) != 1:
                raise HTTPException(409, "Pit settings changed concurrently; retrieve and retry")
            await db.refresh(pit)
        except SQLAlchemyError:
            await db.rollback()
            raise HTTPException(503, "Settings storage unavailable") from None
        except Exception:
            await db.rollback()
            raise

    try:
        await db.flush()
        record_change(
            db,
            actor=session.user,
            pit_id=pit_id,
            resource="pit_config",
            before=before,
            after=pit_values(pit),
        )
        await db.commit()
        await db.refresh(pit)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Pit settings changed concurrently; retrieve and retry") from None
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(503, "Settings storage unavailable") from None
    return PitConfigResponse.model_validate(pit)
