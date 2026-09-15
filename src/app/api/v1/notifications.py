"""Notification center endpoints conforming to proposal/openapi.yaml."""

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session
from src.app.schemas.notification import Notification, NotificationPage, NotificationUpdate
from src.app.services.notification_service import notification_service

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get(
    "",
    response_model=NotificationPage,
    summary="List notifications for the current user",
)
async def list_notifications(
    unread_only: bool = Query(default=False, alias="unreadOnly", description="Filter unread only"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, alias="pageSize", description="Page size"),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationPage:
    """List paginated notifications ordered by occurredAt descending."""
    return await notification_service.list_notifications(
        unread_only=unread_only,
        page=page,
        page_size=page_size,
        db=db,
    )


@router.patch(
    "/{notificationId}",
    response_model=Notification,
    summary="Update the current user's notification state",
)
async def update_notification(
    payload: NotificationUpdate,
    notification_id: str = Path(..., alias="notificationId", description="Notification identifier"),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    db: AsyncSession = Depends(get_db_session),
) -> Notification:
    """Mark a single notification as read."""
    updated = await notification_service.update_notification(
        notification_id=notification_id,
        read=payload.read,
        db=db,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification '{notification_id}' not found",
        )
    return updated


@router.post(
    "/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark all current notifications as read",
)
async def mark_all_notifications_read(
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Mark all unread notifications as read."""
    await notification_service.mark_all_as_read(db=db)
