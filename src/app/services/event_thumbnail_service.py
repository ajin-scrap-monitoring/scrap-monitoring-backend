"""Read shared clip thumbnails without sharing private export requests or creating jobs."""

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.media_job import MediaClipJob
from src.app.schemas.media_service import ClipDetail, event_time_utc
from src.app.schemas.thumbnail import ClipThumbnailInfo
from src.app.services.event_export_service import (
    CONTEXT_KEY,
    EXPORT_CATEGORY,
    export_context,
    export_source,
    validate_range_snapshot,
)
from src.app.services.event_recording_service import event_reference
from src.app.services.media_client import MediaServiceError
from src.app.services.thumbnail_service import thumbnail

MAX_CANDIDATES = 50
MAX_MEDIA_CANDIDATES = 3


async def event_thumbnail(
    db: AsyncSession, event_id: str, pit_id: str | None, before: int, after: int
) -> ClipThumbnailInfo | None:
    """The current event window and live parent both have to match a saved export.

    Clip/image access is already shared by authenticated users. Export identities,
    aliases and status remain private to the requesting account, with no DB writes.
    """
    event, now = await event_reference(db, event_id, pit_id, before, after)
    if event.windowState not in ("CLOSED", "POINT") or event.windowTo is None:
        return None
    settings = get_settings()
    context = MediaClipJob.snapshot[CONTEXT_KEY]
    jobs = await db.scalars(
        select(MediaClipJob)
        .where(
            MediaClipJob.category == EXPORT_CATEGORY,
            MediaClipJob.pit_id == event.pitId,
            MediaClipJob.camera_id == settings.MEDIA_CAMERA_ID,
            MediaClipJob.status == "ready",
            MediaClipJob.clip_id.is_not(None),
            context["event_id"].as_string() == event_id,
            context["source"].as_string() == export_source(),
            context["before"].as_integer() == before,
            context["after"].as_integer() == after,
        )
        .order_by(MediaClipJob.id.desc())
        .limit(MAX_CANDIDATES)
    )
    attempted = 0
    try:
        # All fallback parents and info requests share the existing media time budget.
        async with asyncio.timeout(settings.MEDIA_REQUEST_TIMEOUT_SECONDS):
            for job in jobs:
                try:
                    stored = export_context(job)
                    identifier = UUID(job.clip_id or "")
                except (MediaServiceError, ValueError):
                    continue
                if (
                    stored.start != event_time_utc(event.windowFrom)
                    or stored.end != event_time_utc(event.windowTo)
                    or stored.start >= now
                ):
                    continue

                def validate_parent(parent: ClipDetail, job: MediaClipJob = job) -> None:
                    if parent.event_id != job.event_id:
                        raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
                    validate_range_snapshot(job, parent)
                    if parent.status != "ready":
                        raise MediaServiceError(409, "CLIP_NOT_READY")

                attempted += 1
                try:
                    _, info = await thumbnail(identifier, validate_clip=validate_parent)
                except MediaServiceError as exc:
                    # Only a missing/expired source permits trying another existing clip.
                    if exc.status_code not in (404, 410):
                        raise
                    if attempted >= MAX_MEDIA_CANDIDATES:
                        return None
                    continue
                assert isinstance(info, ClipThumbnailInfo)
                return info
    except TimeoutError as exc:
        raise MediaServiceError(504, "MEDIA_TIMEOUT", retryable=True) from exc
    return None
