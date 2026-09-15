"""Service for querying recorded media segments metadata."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media import MediaIndex
from src.app.schemas.history import EventCategory, PageMetadata
from src.app.schemas.recording import Recording, RecordingPage


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
        records = res.scalars().all()

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
        r = res.scalar_one_or_none()
        if not r:
            return None

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
