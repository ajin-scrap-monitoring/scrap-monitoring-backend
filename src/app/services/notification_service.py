"""Notification service for dashboard header notification center."""

import logging
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import DateTime, and_, exists, func, literal, select
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.log import AlertLog
from src.app.models.notification_read import NotificationRead
from src.app.schemas.history import EventType, PageMetadata
from src.app.schemas.notification import Notification, NotificationPage, NotificationSeverity

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has UTC timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_notification_id(notification_id: str) -> int | None:
    """Parse integer ID from 'notif-123' or '123'."""
    raw = notification_id.strip()
    if raw.startswith("notif-"):
        raw = raw[6:]
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        return None
    value = int(raw)
    return value if 1 <= value <= 2147483647 else None


def _format_notification_title(log: AlertLog) -> str:
    """Derive user-friendly notification title from alert log."""
    if "테스트 알림" in log.message:
        return "테스트 알림 발송"
    if log.fill_ratio_percent >= 85.0:
        return "스크랩 수거 필요 알림 (위험 임계치 초과)"
    if log.fill_ratio_percent >= 75.0:
        return "스크랩 적재율 경고 알림"
    return "시스템 운영 알림"


def _read_insert(db: AsyncSession) -> PGInsert | SQLiteInsert:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite_insert(NotificationRead)
    if dialect == "postgresql":
        return pg_insert(NotificationRead)
    raise HTTPException(503, "Unsupported notification storage")


def _notification(log: AlertLog, read_at: datetime | None) -> Notification:
    return Notification(
        id=f"notif-{log.id}",
        type=cast(EventType, log.event_type or "collection_required"),
        severity=cast(NotificationSeverity, log.severity or "warning"),
        title=_format_notification_title(log),
        detail=log.message,
        occurredAt=_ensure_utc(log.triggered_at),
        readAt=_ensure_utc(read_at) if read_at else None,
    )


class NotificationService:
    """Handles notification query and read status updates."""

    async def list_notifications(
        self,
        unread_only: bool,
        page: int,
        page_size: int,
        db: AsyncSession,
        username: str,
    ) -> NotificationPage:
        """Query paginated alert notifications with unread count."""
        joined = select(AlertLog, NotificationRead.read_at).outerjoin(
            NotificationRead,
            and_(NotificationRead.alert_id == AlertLog.id, NotificationRead.username == username),
        )
        # 1. Total unread count
        unread_query = joined.where(NotificationRead.alert_id.is_(None))
        unread_count = (
            await db.scalar(select(func.count()).select_from(unread_query.subquery())) or 0
        )

        # 2. Filtered list query
        query = unread_query if unread_only else joined
        # Count total items matching filter
        total_items = await db.scalar(select(func.count()).select_from(query.subquery())) or 0

        # 3. Pagination & ordering (latest first)
        query = (
            query.order_by(AlertLog.triggered_at.desc(), AlertLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await db.execute(query)).all()
        items = [_notification(log, read_at) for log, read_at in rows]
        total_pages = (total_items + page_size - 1) // page_size if total_items > 0 else 0
        page_meta = PageMetadata(
            page=page,
            pageSize=page_size,
            totalItems=total_items,
            totalPages=total_pages,
        )
        return NotificationPage(items=items, page=page_meta, unreadCount=unread_count)

    async def update_notification(
        self,
        notification_id: str,
        read: bool,
        db: AsyncSession,
        username: str,
    ) -> Notification | None:
        """Mark single notification as read."""
        log_id = _parse_notification_id(notification_id)
        if log_id is None:
            return None
        try:
            log = await db.get(AlertLog, log_id)
            if log is None:
                return None
            if read:
                await db.execute(
                    _read_insert(db)
                    .values(
                        username=username,
                        alert_id=log_id,
                        read_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[NotificationRead.username, NotificationRead.alert_id]
                    )
                )
            read_at = await db.scalar(
                select(NotificationRead.read_at).where(
                    NotificationRead.username == username,
                    NotificationRead.alert_id == log_id,
                )
            )
            result = _notification(log, read_at)
            await db.commit()
            return result
        except SQLAlchemyError:
            await db.rollback()
            raise HTTPException(503, "Notification storage unavailable") from None

    async def mark_all_as_read(self, db: AsyncSession, username: str) -> int:
        """Mark all unread notifications as read."""
        now = datetime.now(UTC)
        already_read = exists().where(
            NotificationRead.username == username,
            NotificationRead.alert_id == AlertLog.id,
        )
        # One statement captures the currently visible alerts without loading every
        # row into Python. Concurrent single/all reads preserve the first timestamp.
        source = select(
            literal(username),
            AlertLog.id,
            literal(now, type_=DateTime(timezone=True)),
            literal(now, type_=DateTime(timezone=True)),
        ).where(~already_read)
        stmt = (
            _read_insert(db)
            .from_select(
                ["username", "alert_id", "read_at", "created_at"],
                source,
            )
            .on_conflict_do_nothing(
                index_elements=[NotificationRead.username, NotificationRead.alert_id]
            )
        )
        try:
            res = await db.execute(stmt)
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
            raise HTTPException(503, "Notification storage unavailable") from None
        row_count = getattr(res, "rowcount", 0)
        updated_count: int = int(row_count) if row_count is not None else 0
        logger.info("Marked %d notifications as read", updated_count)
        return updated_count


notification_service = NotificationService()
