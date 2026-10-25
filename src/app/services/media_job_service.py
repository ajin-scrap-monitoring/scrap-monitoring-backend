"""Durable event recording requests and recoverable polling of media clip state."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.db.session import get_sessionmaker
from src.app.models.media_job import MediaClipJob
from src.app.schemas.media_service import ClipDetail, ClipReceipt, ClipRequest
from src.app.services.media_client import MediaServiceError, media_client
from src.app.services.v2_event_clip_service import AUTO_CATEGORIES, automatic_request_block

logger = logging.getLogger(__name__)


async def enqueue_event_clip(
    db: AsyncSession, event_id: str, pit_id: str, category: str, occurred_at: datetime
) -> MediaClipJob | None:
    """Insert in the event transaction; media failures cannot block sensor collection.

    One configured camera currently covers one pit. Events for other pits are not
    silently attributed to this camera. No upstream I/O happens in this function.
    """
    settings = get_settings()
    if not settings.MEDIA_SERVICE_URL or pit_id != settings.MEDIA_PIT_ID:
        return None
    existing = await db.scalar(
        select(MediaClipJob).where(
            MediaClipJob.event_id == event_id, MediaClipJob.camera_id == settings.MEDIA_CAMERA_ID
        )
    )
    if existing:
        return existing
    job = MediaClipJob(
        event_id=event_id,
        camera_id=settings.MEDIA_CAMERA_ID,
        pit_id=pit_id,
        category=category,
        occurred_at=occurred_at,
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
    )
    db.add(job)
    await db.flush()
    return job


def apply_snapshot(job: MediaClipJob, snapshot: ClipReceipt) -> None:
    """Keep the exact identity and retain the last known remote metadata."""
    if snapshot.event_id != job.event_id or snapshot.camera_id != job.camera_id:
        raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
    if job.clip_id and job.clip_id != str(snapshot.clip_id):
        raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
    job.clip_id = str(snapshot.clip_id)
    job.status = snapshot.status
    origin = (
        {
            key: value
            for key, value in (job.snapshot or {}).items()
            if key in ("_v2Context", "_v2Simulated", "_v2Collection")
        }
        if job.category in AUTO_CATEGORIES
        else {}
    )
    job.snapshot = {**snapshot.model_dump(mode="json"), **origin}
    job.error_code = None
    # Retained ready files can later expire or be deleted outside the backend.
    # They are refreshed by explicit reads, not polled every two seconds forever.
    job.next_attempt_at = (
        datetime.now(UTC) + timedelta(seconds=get_settings().MEDIA_POLL_INTERVAL_SECONDS)
        if not isinstance(snapshot, ClipDetail) or job.status in ("collecting", "processing")
        else None
    )


async def process_job(job: MediaClipJob, db: AsyncSession) -> int:
    """Dispatch or poll one persisted job, recording failures before returning."""
    job.attempts += 1
    try:
        if job.clip_id:
            snapshot: ClipReceipt = await media_client.get_clip(UUID(job.clip_id))
            status_code = 200
        else:
            if job.category in AUTO_CATEGORIES:
                blocked = automatic_request_block(job, datetime.now(UTC))
                if blocked:
                    # A lost create response may already have produced a clip. Recover
                    # it by GET, never by a late POST which could film the wrong time.
                    if job.attempts > 1 and blocked != "MEDIA_CLIP_SOURCE_CHANGED":
                        page = await media_client.list_clips(
                            {"camera_id": job.camera_id, "event_id": job.event_id, "limit": 1}
                        )
                        if page.items:
                            apply_snapshot(job, page.items[0])
                            await db.commit()
                            return 200
                    raise MediaServiceError(409, blocked)
            status_code, snapshot = await media_client.create_clip(
                ClipRequest(event_id=job.event_id, camera_id=job.camera_id)
            )
        apply_snapshot(job, snapshot)
        await db.commit()
        return status_code
    except MediaServiceError as exc:
        job.error_code = exc.code
        if exc.retryable:
            job.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(30, 2 ** min(job.attempts - 1, 5))
            )
        else:
            # Keep local delivery failures distinct from remote encoding failure.
            job.next_attempt_at = None
            job.status = "blocked"
        await db.commit()
        raise


class MediaJobWorker:
    """One worker per application process, intended for the existing single-worker deployment."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self._v2_event_token: str | None = None

    def start(self) -> None:
        if get_settings().MEDIA_SERVICE_URL and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await media_client.close()

    async def run_once(self, db: AsyncSession) -> None:
        """Bound each sweep; next_attempt_at survives application restarts."""
        async with self.lock:
            if get_settings().MEDIA_V2_EVENT_CLIPS_ENABLED:
                from src.app.services.v2_event_clip_service import queue_recent_events

                self._v2_event_token = self._v2_event_token or uuid4().hex
                await queue_recent_events(db, self._v2_event_token, datetime.now(UTC))
            else:
                self._v2_event_token = None
        ids = (
            await db.scalars(
                select(MediaClipJob.id)
                .where(MediaClipJob.next_attempt_at <= datetime.now(UTC))
                .order_by(MediaClipJob.next_attempt_at, MediaClipJob.id)
                .limit(20)
            )
        ).all()
        for job_id in ids:
            # Release between jobs so control requests do not wait for the entire batch.
            async with self.lock:
                job = await db.scalar(
                    select(MediaClipJob)
                    .where(
                        MediaClipJob.id == job_id, MediaClipJob.next_attempt_at <= datetime.now(UTC)
                    )
                    .execution_options(populate_existing=True)
                )
                if job is None:
                    continue
                try:
                    await process_job(job, db)
                except MediaServiceError as exc:
                    logger.warning("Media job %s deferred: %s", job.id, exc.code)

    async def _run(self) -> None:
        while True:
            try:
                async with get_sessionmaker()() as db:
                    await self.run_once(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log SQL parameters or credentials from an exception string.
                logger.error("Media job worker sweep failed; retrying on the next sweep")
            await asyncio.sleep(get_settings().MEDIA_POLL_INTERVAL_SECONDS)


media_job_worker = MediaJobWorker()


def job_snapshot(job: MediaClipJob) -> ClipReceipt | ClipDetail | None:
    if job.snapshot is None or (
        job.category in AUTO_CATEGORIES
        and "_v2Context" in job.snapshot
        and "clip_id" not in job.snapshot
    ):
        return None
    if "recording_policy" in job.snapshot:
        return ClipDetail.model_validate(job.snapshot)
    return ClipReceipt.model_validate(job.snapshot)
