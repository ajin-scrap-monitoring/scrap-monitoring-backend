"""Service for querying load history and operational events."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.metric import ScrapMetric
from src.app.schemas.history import (
    CollectionThresholdSample,
    Event,
    EventPage,
    HistoryEventMarker,
    LoadHistory,
    PageMetadata,
)
from src.app.schemas.monitoring import LoadSample


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has UTC timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class HistoryService:
    """Provides representative load percentage history and paginated events."""

    async def get_load_history(
        self,
        pit_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        category: str,
        db: AsyncSession,
    ) -> LoadHistory:
        """Query time-series load samples and event markers within interval."""
        now = datetime.now(UTC)
        settings = get_settings()

        effective_to = _ensure_utc(to_time) if to_time else now
        effective_from = _ensure_utc(from_time) if from_time else (effective_to - timedelta(days=7))

        duration_seconds = max(60, int((effective_to - effective_from).total_seconds()))
        if duration_seconds <= 86400:
            bucket_seconds = 300  # 5 minutes
        elif duration_seconds <= 86400 * 7:
            bucket_seconds = 3600  # 1 hour
        else:
            bucket_seconds = 86400  # 1 day

        # 1. Fetch metrics in range
        metric_stmt = (
            select(ScrapMetric)
            .where(
                ScrapMetric.pit_id == pit_id,
                ScrapMetric.measured_at >= effective_from,
                ScrapMetric.measured_at <= effective_to,
            )
            .order_by(ScrapMetric.measured_at.asc())
        )
        metric_res = await db.execute(metric_stmt)
        metrics = metric_res.scalars().all()

        samples = [
            LoadSample(
                measuredAt=_ensure_utc(m.measured_at),
                valuePercent=round(m.fill_ratio_percent, 1),
                valid=m.is_valid,
            )
            for m in metrics
        ]

        # 2. Fetch pit config for threshold
        cfg_stmt = select(PitConfig).where(PitConfig.id == pit_id)
        cfg_res = await db.execute(cfg_stmt)
        pit_cfg = cfg_res.scalar_one_or_none()

        crit_th = (
            pit_cfg.critical_threshold_percent if pit_cfg else settings.DEFAULT_CRITICAL_THRESHOLD
        )
        total_depth = pit_cfg.total_depth_cm if pit_cfg else 300.0

        collection_thresholds = [
            CollectionThresholdSample(effectiveAt=effective_from, valuePercent=crit_th)
        ]

        # 3. Build Event Markers
        event_markers: list[HistoryEventMarker] = []

        # Collections
        if category in ("all", "collection"):
            col_stmt = (
                select(CollectionLog)
                .where(
                    CollectionLog.pit_id == pit_id,
                    CollectionLog.completed_at >= effective_from,
                    CollectionLog.completed_at <= effective_to,
                )
                .order_by(CollectionLog.completed_at.asc())
            )
            col_res = await db.execute(col_stmt)
            for c in col_res.scalars().all():
                res_pct = min(100.0, max(0.0, (c.residual_height_cm / total_depth) * 100.0))
                event_markers.append(
                    HistoryEventMarker(
                        id=f"marker-col-{c.id}",
                        type="collection_completed",
                        category="collection",
                        severity="info",
                        title="수거 완료",
                        detail=f"{c.operator_name} 수거 완료 ({c.residual_height_cm}cm)",
                        occurredAt=_ensure_utc(c.completed_at),
                        valuePercent=round(res_pct, 1),
                    )
                )

        # Alerts
        if category in ("all", "alert"):
            alt_stmt = (
                select(AlertLog)
                .where(
                    AlertLog.pit_id == pit_id,
                    AlertLog.triggered_at >= effective_from,
                    AlertLog.triggered_at <= effective_to,
                )
                .order_by(AlertLog.triggered_at.asc())
            )
            alt_res = await db.execute(alt_stmt)
            for a in alt_res.scalars().all():
                event_markers.append(
                    HistoryEventMarker(
                        id=f"marker-alt-{a.id}",
                        type="collection_required",
                        category="alert",
                        severity="error",
                        title="수거 필요",
                        detail=a.message,
                        occurredAt=_ensure_utc(a.triggered_at),
                        valuePercent=round(a.fill_ratio_percent, 1),
                    )
                )

        # Measurement Errors
        if category in ("all", "error"):
            for m in metrics:
                if not m.is_valid:
                    event_markers.append(
                        HistoryEventMarker(
                            id=f"marker-err-{m.id}",
                            type="measurement_error",
                            category="error",
                            severity="error",
                            title="측정 오류",
                            detail="센서 데이터 측정 이상",
                            occurredAt=_ensure_utc(m.measured_at),
                            valuePercent=round(m.fill_ratio_percent, 1),
                        )
                    )

        event_markers.sort(key=lambda x: x.occurredAt)

        return LoadHistory(
            from_time=effective_from,
            to_time=effective_to,
            bucketSeconds=bucket_seconds,
            samples=samples,
            collectionThresholds=collection_thresholds,
            eventMarkers=event_markers,
        )

    async def list_events(
        self,
        pit_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        category: str,
        page: int,
        pageSize: int,
        db: AsyncSession,
    ) -> EventPage:
        """List paginated operational events ordered by occurredAt descending."""
        events: list[Event] = []

        # 1. Collections
        if category in ("all", "collection"):
            col_stmt = select(CollectionLog).where(CollectionLog.pit_id == pit_id)
            if from_time:
                col_stmt = col_stmt.where(CollectionLog.completed_at >= _ensure_utc(from_time))
            if to_time:
                col_stmt = col_stmt.where(CollectionLog.completed_at <= _ensure_utc(to_time))
            col_stmt = col_stmt.order_by(desc(CollectionLog.completed_at))

            col_res = await db.execute(col_stmt)
            for c in col_res.scalars().all():
                events.append(
                    Event(
                        id=f"evt-col-{c.id}",
                        type="collection_completed",
                        category="collection",
                        severity="info",
                        status="completed",
                        title="수거 완료",
                        detail=f"{c.operator_name}: {c.notes or '스크랩 수거 완료'}",
                        occurredAt=_ensure_utc(c.completed_at),
                        resolvedAt=_ensure_utc(c.completed_at),
                        recordingId=None,
                    )
                )

        # 2. Alerts
        if category in ("all", "alert"):
            alt_stmt = select(AlertLog).where(AlertLog.pit_id == pit_id)
            if from_time:
                alt_stmt = alt_stmt.where(AlertLog.triggered_at >= _ensure_utc(from_time))
            if to_time:
                alt_stmt = alt_stmt.where(AlertLog.triggered_at <= _ensure_utc(to_time))
            alt_stmt = alt_stmt.order_by(desc(AlertLog.triggered_at))

            alt_res = await db.execute(alt_stmt)
            for a in alt_res.scalars().all():
                events.append(
                    Event(
                        id=f"evt-alt-{a.id}",
                        type="collection_required",
                        category="alert",
                        severity="error",
                        status="resolved",
                        title="수거 필요",
                        detail=a.message,
                        occurredAt=_ensure_utc(a.triggered_at),
                        resolvedAt=None,
                        recordingId=None,
                    )
                )

        # 3. Measurement Errors
        if category in ("all", "error"):
            err_stmt = select(ScrapMetric).where(
                ScrapMetric.pit_id == pit_id,
                ScrapMetric.is_valid.is_(False),
            )
            if from_time:
                err_stmt = err_stmt.where(ScrapMetric.measured_at >= _ensure_utc(from_time))
            if to_time:
                err_stmt = err_stmt.where(ScrapMetric.measured_at <= _ensure_utc(to_time))
            err_stmt = err_stmt.order_by(desc(ScrapMetric.measured_at))

            err_res = await db.execute(err_stmt)
            for m in err_res.scalars().all():
                events.append(
                    Event(
                        id=f"evt-err-{m.id}",
                        type="measurement_error",
                        category="error",
                        severity="error",
                        status="resolved",
                        title="센서 데이터 이상",
                        detail="LiDAR 측정 센서 이상 감지",
                        occurredAt=_ensure_utc(m.measured_at),
                        resolvedAt=None,
                        recordingId=None,
                    )
                )

        # Sort all events by occurredAt descending
        events.sort(key=lambda e: e.occurredAt, reverse=True)

        total_items = len(events)
        total_pages = (total_items + pageSize - 1) // pageSize if total_items > 0 else 0

        start_idx = (page - 1) * pageSize
        end_idx = start_idx + pageSize
        paged_items = events[start_idx:end_idx]

        return EventPage(
            items=paged_items,
            page=PageMetadata(
                page=page,
                pageSize=pageSize,
                totalItems=total_items,
                totalPages=total_pages,
            ),
        )


history_service = HistoryService()
