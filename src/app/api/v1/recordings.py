"""Recordings endpoints conforming to frontend dashboard proposal."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session
from src.app.schemas.recording import Recording, RecordingPage
from src.app.services.recording_service import recording_service

router = APIRouter(prefix="/recordings", tags=["Recordings"])


@router.get(
    "",
    response_model=RecordingPage,
    summary="List recordings overlapping the requested interval",
)
async def list_recordings(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    category: Literal["all", "collection", "alert", "error"] = Query(
        default="all", alias="category"
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, alias="pageSize", description="Page size"),
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> RecordingPage:
    """List paginated recordings with filter criteria."""
    return await recording_service.list_recordings(
        pit_id=pit_id,
        from_time=from_time,
        to_time=to_time,
        category=category,
        page=page,
        pageSize=page_size,
        db=db,
    )


@router.get(
    "/{recordingId}",
    response_model=Recording,
    summary="Get recording metadata by identifier",
)
async def get_recording(
    recording_id: str = Path(..., alias="recordingId", description="Recording segment identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> Recording:
    """Get single recording metadata by identifier."""
    rec = await recording_service.get_recording(recording_id=recording_id, db=db)
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording '{recording_id}' not found",
        )
    return rec
