"""Camera and Media Integration endpoints (Metadata & Authorization only)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session
from src.app.models.media import MediaIndex
from src.app.schemas.media import (
    MediaIndexResponse,
    MediaRecordingCompleteRequest,
    MediaSessionVerifyResponse,
)

router = APIRouter(prefix="/media", tags=["Media Integration"])


@router.post(
    "/recordings/complete",
    response_model=MediaIndexResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record video segment metadata completion webhook",
)
async def recording_complete_webhook(
    payload: MediaRecordingCompleteRequest,
    db: AsyncSession = Depends(get_db_session),
) -> MediaIndexResponse:
    """Receive recording completion notification from Media Service and index metadata.

    Note: Backend only indexes metadata; video bytes are stored in Media Service.
    """
    media_entry = MediaIndex(
        segment_id=payload.segment_id,
        pit_id=payload.pit_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        file_path=payload.file_path,
        file_size_bytes=payload.file_size_bytes,
        duration_seconds=payload.duration_seconds,
    )
    db.add(media_entry)
    await db.commit()
    await db.refresh(media_entry)
    return MediaIndexResponse.model_validate(media_entry)


@router.get(
    "/recordings",
    response_model=list[MediaIndexResponse],
    summary="Query recorded media segment indices",
)
async def list_recordings(
    pit_id: str = Query(default="pit-01"),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[MediaIndexResponse]:
    """Query recorded video segments metadata by pit and time range."""
    stmt = select(MediaIndex).where(MediaIndex.pit_id == pit_id)
    if start_time:
        stmt = stmt.where(MediaIndex.start_time >= start_time)
    if end_time:
        stmt = stmt.where(MediaIndex.end_time <= end_time)

    stmt = stmt.order_by(desc(MediaIndex.start_time)).limit(limit)
    result = await db.execute(stmt)
    records = result.scalars().all()
    return [MediaIndexResponse.model_validate(r) for r in records]


@router.get(
    "/session/verify",
    response_model=MediaSessionVerifyResponse,
    summary="Verify client session permission for live video viewing",
)
async def verify_media_session(
    pit_id: str = Query(default="pit-01"),
) -> MediaSessionVerifyResponse:
    """Verify whether a client is authorized to connect to the Camera Media Service stream."""
    # Production: check user JWT token or role. Dev: grant authorization
    return MediaSessionVerifyResponse(
        authorized=True,
        stream_url=f"/media/live/{pit_id}.m3u8",
        message="Session verified for live camera stream access",
    )
