"""Notification service for dashboard header notification center."""

import logging
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.log import AlertLog
from src.app.schemas.history import EventType, PageMetadata
from src.app.schemas.notification import Notification, NotificationPage, NotificationSeverity

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has UTC timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _parse_notification_id(notification_id: str) -> int | None:
    """Parse integer ID from 'notif-123' or '123'."""
    raw = notification_id.strip()
    if raw.startswith("notif-"):
        raw = raw[6:]
    try:
        return int(raw)
    except ValueError:
        return None


def _format_notification_title(log: AlertLog) -> str:
    """Derive user-friendly notification title from alert log."""
    if "테스트 알림" in log.message:
        return "테스트 알림 발송"
    if log.fill_ratio_percent >= 85.0:
        return "스크랩 수거 필요 알림 (위험 임계치 초과)"
    if log.fill_ratio_percent >= 75.0:
        return "스크랩 적재율 경고 알림"
    return "시스템 운영 알림"


class NotificationService:
    """Handles notification query and read status updates."""

    async def list_notifications(
        self,
        unread_only: bool,
        page: int,
        page_size: int,
        db: AsyncSession,
    ) -> NotificationPage:
        """Query paginated alert notifications with unread count."""
        # 1. Total unread count
        unread_count_stmt = (
            select(func.count()).select_from(AlertLog).where(AlertLog.read_at.is_(None))
        )
        unread_count_res = await db.execute(unread_count_stmt)
        unread_count = unread_count_res.scalar() or 0

        # 2. Filtered list query
        query = select(AlertLog)
        if unread_only:
            query = query.where(AlertLog.read_at.is_(None))

        # Count total items matching filter
        count_stmt = select(func.count()).select_from(query.subquery())
        count_res = await db.execute(count_stmt)
        total_items = count_res.scalar() or 0

        # 3. Pagination & ordering (latest first)
        query = (
            query.order_by(AlertLog.triggered_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        res = await db.execute(query)
        logs = res.scalars().all()

        items: list[Notification] = []
        for log in logs:
            event_type = cast(EventType, log.event_type or "collection_required")
            severity = cast(NotificationSeverity, log.severity or "warning")
            title = _format_notification_title(log)
            read_at = _ensure_utc(log.read_at) if log.read_at else None

            items.append(
                Notification(
                    id=f"notif-{log.id}",
                    type=event_type,
                    severity=severity,
                    title=title,
                    detail=log.message,
                    occurredAt=_ensure_utc(log.triggered_at),
                    readAt=read_at,
                )
            )

        total_pages = (total_items + page_size - 1) // page_size if total_items > 0 else 0
        page_meta = PageMetadata(
            page=page,
            pageSize=page_size,
            totalItems=total_items,
            totalPages=total_pages,
        )

        return NotificationPage(
            items=items,
            page=page_meta,
            unreadCount=unread_count,
        )

    async def update_notification(
        self,
        notification_id: str,
        read: bool,
        db: AsyncSession,
    ) -> Notification | None:
        """Mark single notification as read."""
        log_id = _parse_notification_id(notification_id)
        if log_id is None:
            return None

        stmt = select(AlertLog).where(AlertLog.id == log_id)
        res = await db.execute(stmt)
        log = res.scalar_one_or_none()
        if not log:
            return None

        if read and log.read_at is None:
            log.read_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(log)

        event_type = cast(EventType, log.event_type or "collection_required")
        severity = cast(NotificationSeverity, log.severity or "warning")
        title = _format_notification_title(log)
        read_at = _ensure_utc(log.read_at) if log.read_at else None

        return Notification(
            id=f"notif-{log.id}",
            type=event_type,
            severity=severity,
            title=title,
            detail=log.message,
            occurredAt=_ensure_utc(log.triggered_at),
            readAt=read_at,
        )

    async def mark_all_as_read(self, db: AsyncSession) -> int:
        """Mark all unread notifications as read."""
        stmt = update(AlertLog).where(AlertLog.read_at.is_(None)).values(read_at=datetime.now(UTC))
        res = await db.execute(stmt)
        await db.commit()
        row_count = getattr(res, "rowcount", 0)
        updated_count: int = int(row_count) if row_count is not None else 0
        logger.info("Marked %d notifications as read", updated_count)
        return updated_count


notification_service = NotificationService()
