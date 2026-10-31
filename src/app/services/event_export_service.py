"""Persist a user's event window before requesting an idempotent archive extraction."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.media_job import MediaClipJob
from src.app.schemas.event_export import EventExportRequest, EventExportResponse
from src.app.schemas.media_service import ClipDetail, ClipReceipt, RangeClipRequest, event_time_utc
from src.app.services.edge_v2_service import digest
from src.app.services.event_recording_service import event_reference
from src.app.services.media_client import MediaServiceError, media_client

EXPORT_CATEGORY = "event_export"
CONTEXT_KEY = "_rangeExport"


class ExportContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    owner: str
    request_id: UUID
    event_id: str
    before: int
    after: int
    start: AwareDatetime
    end: AwareDatetime
    source: str
    retry_failed: bool = False


def export_source() -> str:
    settings = get_settings()
    return digest([settings.MEDIA_SERVICE_URL, settings.MEDIA_CAMERA_ID, settings.MEDIA_PIT_ID])


def export_identifier(owner: str, request_id: UUID) -> str:
    return "event-export-" + digest([owner, str(request_id)])


def export_context(job: MediaClipJob) -> ExportContext:
    try:
        context = ExportContext.model_validate((job.snapshot or {}).get(CONTEXT_KEY))
    except ValidationError as exc:
        raise MediaServiceError(409, "MEDIA_RANGE_CONTEXT_INVALID") from exc
    settings = get_settings()
    if context.source != export_source() or (
        job.camera_id != settings.MEDIA_CAMERA_ID or job.pit_id != settings.MEDIA_PIT_ID
    ):
        raise MediaServiceError(409, "MEDIA_CLIP_SOURCE_CHANGED")
    if (
        context.version != 1
        or job.event_id != export_identifier(context.owner, context.request_id)
        or context.start >= context.end
        or not 0 < (context.end - context.start).total_seconds() <= 3600
    ):
        raise MediaServiceError(409, "MEDIA_RANGE_CONTEXT_INVALID")
    return context


def range_request(job: MediaClipJob) -> RangeClipRequest | None:
    if job.category != EXPORT_CATEGORY:
        return None
    context = export_context(job)
    return RangeClipRequest(
        event_id=job.event_id,
        camera_id=job.camera_id,
        start_at=context.start,
        end_at=context.end,
        retry_failed=context.retry_failed,
    )


def validate_range_snapshot(job: MediaClipJob, snapshot: ClipReceipt) -> None:
    if job.category != EXPORT_CATEGORY:
        return
    context = export_context(job)
    if (
        snapshot.request_mode != "time_range"
        or snapshot.attempt_count is None
        or snapshot.requested_start_at != context.start
        or snapshot.requested_end_at != context.end
        or snapshot.event_at is not None
    ):
        raise MediaServiceError(502, "MEDIA_RANGE_BASIS_MISMATCH")
    previous_attempt = (job.snapshot or {}).get("attempt_count")
    if isinstance(previous_attempt, int) and snapshot.attempt_count < previous_attempt:
        raise MediaServiceError(502, "MEDIA_RANGE_BASIS_MISMATCH")
    if isinstance(snapshot, ClipDetail):
        intervals = snapshot.available_intervals + snapshot.gaps
        if any(x.start_at >= x.end_at for x in intervals):
            raise MediaServiceError(502, "MEDIA_RANGE_BASIS_MISMATCH")
        if snapshot.status == "ready" and (
            snapshot.extraction_mode != "stream_copy"
            or not snapshot.size_bytes
            or not snapshot.available_intervals
            or snapshot.actual_start_at is None
            or snapshot.actual_end_at is None
            or snapshot.actual_start_at >= snapshot.actual_end_at
            or snapshot.actual_start_at < context.start - timedelta(seconds=1)
            or snapshot.actual_end_at > context.end + timedelta(seconds=1)
            or any(
                x.start_at < snapshot.actual_start_at or x.end_at > snapshot.actual_end_at
                for x in snapshot.available_intervals
            )
        ):
            raise MediaServiceError(502, "MEDIA_RANGE_BASIS_MISMATCH")


def export_response(job: MediaClipJob) -> EventExportResponse:
    from src.app.services.media_job_service import job_snapshot

    context = export_context(job)
    snapshot = job_snapshot(job)
    result = EventExportResponse(
        requestId=context.request_id,
        eventId=context.event_id,
        status=job.status,
        requestedFrom=context.start,
        requestedTo=context.end,
        statusPath=f"/api/v2/recordings/events/{context.event_id}/exports/{context.request_id}",
        clipId=UUID(job.clip_id) if job.clip_id else None,
        attemptCount=snapshot.attempt_count if snapshot else None,
        errorCode=job.error_code,
    )
    if isinstance(snapshot, ClipDetail):
        result.coverage = snapshot.coverage
        result.actualFrom, result.actualTo = snapshot.actual_start_at, snapshot.actual_end_at
        result.extractionMode, result.sizeBytes = snapshot.extraction_mode, snapshot.size_bytes
        result.expiresAt = snapshot.expires_at
        # Error messages and arbitrary upstream reason text are never exposed here.
        allowed_reasons = {
            "CAMERA_DISCONNECTED",
            "RECORDING_UNAVAILABLE",
            "FUTURE_NOT_RECORDED",
            "CAMERA_UNAVAILABLE",
            "NO_FRAMES_AVAILABLE",
            "RECORDING_EXPIRED",
        }
        result.availableIntervals = [
            x.model_copy(update={"reason_code": None}) for x in snapshot.available_intervals
        ]
        result.gaps = [
            x.model_copy(
                update={
                    "reason_code": x.reason_code
                    if x.reason_code in allowed_reasons
                    else "RECORDING_UNAVAILABLE"
                }
            )
            for x in snapshot.gaps
        ]
        if snapshot.error:
            allowed_errors = {
                "EXPORT_INCOMPATIBLE_MEDIA",
                "RECORDING_BOUNDARY_UNAVAILABLE",
                "INSUFFICIENT_STORAGE",
                "EXPORT_CAPACITY_EXCEEDED",
                "RECORDING_STORAGE_ERROR",
                "NO_FRAMES_AVAILABLE",
                "EXPORT_FAILED",
                "CLIP_PREPARATION_FAILED",
            }
            result.errorCode = (
                snapshot.error.code
                if snapshot.error.code in allowed_errors
                else "EVENT_EXPORT_FAILED"
            )
        if (
            snapshot.status == "ready"
            and snapshot.content_path
            and snapshot.expires_at
            and snapshot.expires_at > datetime.now(UTC)
        ):
            result.downloadPath = f"/media/clips/{snapshot.clip_id}/content?download=true"
    return result


async def find_export(
    db: AsyncSession, owner: str, request_id: UUID, event_id: str
) -> MediaClipJob:
    job = await db.scalar(
        select(MediaClipJob).where(
            MediaClipJob.event_id == export_identifier(owner, request_id),
            MediaClipJob.camera_id == get_settings().MEDIA_CAMERA_ID,
            MediaClipJob.category == EXPORT_CATEGORY,
        )
    )
    if job is None:
        raise HTTPException(404, "EVENT_EXPORT_NOT_FOUND")
    context = export_context(job)
    if context.owner != owner or context.event_id != event_id:
        raise HTTPException(404, "EVENT_EXPORT_NOT_FOUND")
    return job


async def request_export(
    db: AsyncSession, owner: str, event_id: str, payload: EventExportRequest
) -> tuple[int, EventExportResponse]:
    from src.app.services.media_job_service import media_job_worker, process_job

    settings = get_settings()
    if not settings.MEDIA_SERVICE_URL or not settings.MEDIA_API_TOKEN:
        raise MediaServiceError(503, "MEDIA_NOT_CONFIGURED")
    async with media_job_worker.lock:
        job = await db.scalar(
            select(MediaClipJob).where(
                MediaClipJob.event_id == export_identifier(owner, payload.requestId),
                MediaClipJob.camera_id == settings.MEDIA_CAMERA_ID,
            )
        )
        if job is None:
            if payload.retryFailed:
                raise HTTPException(404, "EVENT_EXPORT_NOT_FOUND")
            event, now = await event_reference(
                db, event_id, None, payload.beforeSeconds, payload.afterSeconds
            )
            if event.windowState not in ("CLOSED", "POINT") or event.windowTo is None:
                raise HTTPException(409, "EVENT_WINDOW_NOT_CLOSED")
            start, end = event_time_utc(event.windowFrom), event_time_utc(event.windowTo)
            if not 0 < (end - start).total_seconds() <= 3600:
                raise HTTPException(422, "EVENT_EXPORT_RANGE_TOO_LONG")
            if start >= now:
                raise HTTPException(409, "EVENT_WINDOW_IN_FUTURE")
            context = ExportContext(
                owner=owner,
                request_id=payload.requestId,
                event_id=event_id,
                before=payload.beforeSeconds,
                after=payload.afterSeconds,
                start=start,
                end=end,
                source=export_source(),
            )
            job = MediaClipJob(
                event_id=export_identifier(owner, payload.requestId),
                camera_id=settings.MEDIA_CAMERA_ID,
                pit_id=event.pitId,
                category=EXPORT_CATEGORY,
                occurred_at=event.occurredAt,
                status="pending",
                attempts=0,
                next_attempt_at=now,
                snapshot={CONTEXT_KEY: context.model_dump(mode="json")},
            )
            db.add(job)
            await db.commit()
        else:
            if job.category != EXPORT_CATEGORY:
                raise HTTPException(409, "EVENT_EXPORT_REQUEST_CONFLICT")
            context = export_context(job)
            if (context.owner, context.event_id, context.before, context.after) != (
                owner,
                event_id,
                payload.beforeSeconds,
                payload.afterSeconds,
            ):
                raise HTTPException(409, "EVENT_EXPORT_REQUEST_CONFLICT")
        if payload.retryFailed:
            # Persist retry intent as well: a lost retry response must reuse this key.
            context = export_context(job).model_copy(update={"retry_failed": True})
            job.snapshot = {**(job.snapshot or {}), CONTEXT_KEY: context.model_dump(mode="json")}
            await db.commit()
        code = await process_job(job, db)
        return code, export_response(job)


async def read_export(
    db: AsyncSession, owner: str, event_id: str, request_id: UUID
) -> EventExportResponse:
    from src.app.services.media_job_service import apply_snapshot, media_job_worker

    async with media_job_worker.lock:
        job = await find_export(db, owner, request_id, event_id)
        # A status read may refresh an accepted clip, but never creates/retries one.
        if job.clip_id:
            apply_snapshot(job, await media_client.get_clip(UUID(job.clip_id)))
            await db.commit()
        return export_response(job)
