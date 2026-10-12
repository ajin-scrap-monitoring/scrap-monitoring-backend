"""Service for querying recorded media segments metadata."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media import MediaIndex
from src.app.models.media_job import MediaClipJob
from src.app.schemas.history import EventCategory, PageMetadata
from src.app.schemas.media_service import ClipDetail
from src.app.schemas.recording import Recording, RecordingPage, RecordingStatus
from src.app.services.media_client import media_client
from src.app.services.media_job_service import apply_snapshot, job_snapshot, media_job_worker


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has UTC timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class RecordingService:
    """Queries recording metadata and formats into frontend Recording schema."""

    async def list_recordings(
        self,
        pit_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        category: str,
        page: int,
        pageSize: int,
        db: AsyncSession,
    ) -> RecordingPage:
        """List paginated recordings ordered by startedAt descending."""
        stmt = select(MediaIndex).where(MediaIndex.pit_id == pit_id)
        if from_time:
            stmt = stmt.where(MediaIndex.start_time >= _ensure_utc(from_time))
        if to_time:
            stmt = stmt.where(MediaIndex.end_time <= _ensure_utc(to_time))

        stmt = stmt.order_by(desc(MediaIndex.start_time))
        res = await db.execute(stmt)
        # Legacy indices have no media-v1 clip identity; keep them in standalone mode only.
        records = res.scalars().all() if not get_settings().MEDIA_SERVICE_URL else []

        recordings: list[Recording] = []
        for r in records:
            event_cat: EventCategory = await self._resolve_event_category(
                r.pit_id, r.start_time, r.end_time, db
            )

            # Apply category filter if specified
            if category != "all" and event_cat != category:
                continue

            started_at = _ensure_utc(r.start_time)
            ended_at = _ensure_utc(r.end_time)
            duration = int(r.duration_seconds)

            recordings.append(
                Recording(
                    id=r.segment_id,
                    startedAt=started_at,
                    endedAt=ended_at,
                    durationSeconds=duration,
                    eventCategory=event_cat,
                    title=f"녹화 {started_at.strftime('%Y-%m-%d')}",
                    detail=f"{r.segment_id} 영상 세그먼트",
                    containerFormat="mp4",
                    videoCodec="h264",
                    widthPixels=640,
                    heightPixels=480,
                    frameRate=30.0,
                    sizeBytes=r.file_size_bytes,
                    retainedFrom=started_at,
                    retainedUntil=started_at + timedelta(days=30),
                    status="available",
                    thumbnailPath=f"/media/thumbnails/{r.segment_id}.jpg",
                    contentPath=f"/media/recordings/{r.segment_id}.mp4",
                    downloadPath=f"/media/downloads/{r.segment_id}.mp4",
                )
            )

        # Locally indexed event clips use the existing dashboard's page contract.
        # The separate /media/clips endpoint preserves the remote opaque cursor.
        job_stmt = select(MediaClipJob).where(MediaClipJob.pit_id == pit_id)
        for job in (await db.scalars(job_stmt)).all():
            if job.clip_id is None or (category != "all" and job.category != category):
                continue
            recording = self._format_media_job(job)
            if from_time and recording.endedAt < _ensure_utc(from_time):
                continue
            if to_time and recording.startedAt >= _ensure_utc(to_time):
                continue
            recordings.append(recording)
        recordings.sort(key=lambda recording: (recording.startedAt, recording.id), reverse=True)

        total_items = len(recordings)
        total_pages = (total_items + pageSize - 1) // pageSize if total_items > 0 else 0

        start_idx = (page - 1) * pageSize
        end_idx = start_idx + pageSize
        paged_items = recordings[start_idx:end_idx]

        return RecordingPage(
            items=paged_items,
            page=PageMetadata(
                page=page,
                pageSize=pageSize,
                totalItems=total_items,
                totalPages=total_pages,
            ),
        )

    async def get_recording(self, recording_id: str, db: AsyncSession) -> Recording | None:
        """Get single recording metadata by segment identifier."""
        stmt = select(MediaIndex).where(MediaIndex.segment_id == recording_id)
        res = await db.execute(stmt)
        r = res.scalar_one_or_none() if not get_settings().MEDIA_SERVICE_URL else None
        if not r:
            async with media_job_worker.lock:
                job = await db.scalar(
                    select(MediaClipJob).where(MediaClipJob.clip_id == recording_id)
                )
                if job is None:
                    return None
                # Recheck ready files to reflect deletion or expiry outside the backend.
                detail = await media_client.get_clip(UUID(recording_id))
                apply_snapshot(job, detail)
                await db.commit()
                return self._format_media_job(job)

        event_cat = await self._resolve_event_category(r.pit_id, r.start_time, r.end_time, db)
        started_at = _ensure_utc(r.start_time)
        ended_at = _ensure_utc(r.end_time)

        return Recording(
            id=r.segment_id,
            startedAt=started_at,
            endedAt=ended_at,
            durationSeconds=int(r.duration_seconds),
            eventCategory=event_cat,
            title=f"녹화 {started_at.strftime('%Y-%m-%d')}",
            detail=f"{r.segment_id} 영상 세그먼트",
            containerFormat="mp4",
            videoCodec="h264",
            widthPixels=640,
            heightPixels=480,
            frameRate=30.0,
            sizeBytes=r.file_size_bytes,
            retainedFrom=started_at,
            retainedUntil=started_at + timedelta(days=30),
            status="available",
            thumbnailPath=f"/media/thumbnails/{r.segment_id}.jpg",
            contentPath=f"/media/recordings/{r.segment_id}.mp4",
            downloadPath=f"/media/downloads/{r.segment_id}.mp4",
        )

    def _format_media_job(self, job: MediaClipJob) -> Recording:
        """Do not invent resolution, file size, thumbnails or a retention deadline."""
        snapshot = job_snapshot(job)
        detail = snapshot if isinstance(snapshot, ClipDetail) else None
        started_at = _ensure_utc(
            detail.actual_start_at
            if detail and detail.actual_start_at
            else snapshot.requested_start_at
            if snapshot
            else job.occurred_at
        )
        ended_at = _ensure_utc(
            detail.actual_end_at
            if detail and detail.actual_end_at
            else snapshot.requested_end_at
            if snapshot
            else job.occurred_at
        )
        expires_at = detail.expires_at if detail else None
        remote_status = job.status
        if remote_status == "ready" and expires_at and expires_at <= datetime.now(UTC):
            remote_status = "expired"
        statuses: dict[str, RecordingStatus] = {
            "ready": "available",
            "expired": "expired",
            "failed": "unavailable",
            "deleted": "unavailable",
            "blocked": "unavailable",
        }
        status = statuses.get(remote_status, "processing")
        path = (
            f"/media/clips/{job.clip_id}/content"
            if status == "available" and detail and detail.content_path
            else None
        )
        duration = (
            max(0, int((detail.actual_end_at - detail.actual_start_at).total_seconds()))
            if detail and detail.actual_start_at and detail.actual_end_at
            else None
        )
        category: EventCategory = "alert" if job.category == "alert" else "collection"
        return Recording(
            id=job.clip_id or job.event_id,
            startedAt=started_at,
            endedAt=ended_at,
            durationSeconds=duration,
            eventCategory=category,
            title=f"이벤트 녹화 {started_at.strftime('%Y-%m-%d')}",
            detail=job.event_id,
            widthPixels=None,
            heightPixels=None,
            frameRate=None,
            sizeBytes=None,
            retainedFrom=detail.completed_at if detail else None,
            retainedUntil=expires_at,
            status=status,
            thumbnailPath=None,
            contentPath=path,
            downloadPath=f"{path}?download=true" if path else None,
            mediaStatus=remote_status,
            coverage=detail.coverage if detail else None,
            gaps=[{"start_at": gap.start_at, "end_at": gap.end_at} for gap in detail.gaps]
            if detail
            else [],
            errorCode=detail.error.code if detail and detail.error else job.error_code,
        )

    async def _resolve_event_category(
        self,
        pit_id: str,
        start_time: datetime,
        end_time: datetime,
        db: AsyncSession,
    ) -> EventCategory:
        """Determine whether the segment overlaps with collection, alert, or error event."""
        # 1. Check collection log
        col_stmt = (
            select(CollectionLog)
            .where(
                CollectionLog.pit_id == pit_id,
                CollectionLog.completed_at >= start_time,
                CollectionLog.completed_at <= end_time,
            )
            .limit(1)
        )
        col_res = await db.execute(col_stmt)
        if col_res.scalar_one_or_none():
            return "collection"

        # 2. Check alert log
        alt_stmt = (
            select(AlertLog)
            .where(
                AlertLog.pit_id == pit_id,
                AlertLog.triggered_at >= start_time,
                AlertLog.triggered_at <= end_time,
            )
            .limit(1)
        )
        alt_res = await db.execute(alt_stmt)
        if alt_res.scalar_one_or_none():
            return "alert"

        return "collection"


recording_service = RecordingService()
