"""Administration and alert settings endpoints conforming to proposal/openapi.yaml."""

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.db.session import get_db_session
from src.app.schemas.admin import (
    AlertSettings,
    AlertSettingsUpdate,
    NotificationRecipient,
    NotificationRecipientCreate,
    NotificationRecipientPage,
    NotificationRecipientPatch,
    TestNotificationRequest,
)
from src.app.services.admin_service import admin_service

settings_router = APIRouter(prefix="/settings", tags=["Administration"])
recipients_router = APIRouter(prefix="/notification-recipients", tags=["Administration"])
notifications_admin_router = APIRouter(prefix="/notifications", tags=["Administration"])


@settings_router.get(
    "/alerts",
    response_model=AlertSettings,
    summary="Get alert settings",
)
async def get_alert_settings(
    response: Response,
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> AlertSettings:
    """Get current threshold and alert policy settings."""
    settings_obj, etag = await admin_service.get_alert_settings(pit_id=pit_id, db=db)
    response.headers["ETag"] = etag
    return settings_obj


@settings_router.put(
    "/alerts",
    response_model=AlertSettings,
    summary="Replace alert settings",
)
async def replace_alert_settings(
    payload: AlertSettingsUpdate,
    response: Response,
    pit_id: str = Query(default="pit-01", description="Pit identifier"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db_session),
) -> AlertSettings:
    """Replace threshold and alert policy settings."""
    current, current_etag = await admin_service.get_alert_settings(pit_id=pit_id, db=db)
    if if_match and if_match != current_etag:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Precondition failed: ETag does not match current version",
        )

    updated, new_etag = await admin_service.replace_alert_settings(
        pit_id=pit_id, payload=payload, db=db
    )
    response.headers["ETag"] = new_etag
    return updated


@recipients_router.get(
    "",
    response_model=NotificationRecipientPage,
    summary="List notification recipients",
)
async def list_notification_recipients(
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, alias="pageSize", description="Page size"),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationRecipientPage:
    """List paginated alert notification recipients."""
    return await admin_service.list_recipients(page=page, pageSize=page_size, db=db)


@recipients_router.post(
    "",
    response_model=NotificationRecipient,
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification recipient",
)
async def create_notification_recipient(
    payload: NotificationRecipientCreate,
    response: Response,
    db: AsyncSession = Depends(get_db_session),
) -> NotificationRecipient:
    """Create a new alert notification recipient."""
    item, etag = await admin_service.create_recipient(payload=payload, db=db)
    response.headers["ETag"] = etag
    response.headers["Location"] = f"/api/v1/notification-recipients/{item.id}"
    return item


@recipients_router.patch(
    "/{recipientId}",
    response_model=NotificationRecipient,
    summary="Update a notification recipient",
)
async def update_notification_recipient(
    payload: NotificationRecipientPatch,
    response: Response,
    recipient_id: str = Path(..., alias="recipientId", description="Recipient identifier"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db_session),
) -> NotificationRecipient:
    """Partially update an existing notification recipient."""
    updated = await admin_service.update_recipient(
        recipient_id=recipient_id, payload=payload, db=db
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recipient '{recipient_id}' not found",
        )
    item, etag = updated
    response.headers["ETag"] = etag
    return item


@notifications_admin_router.post(
    "/test",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a test notification",
)
async def send_test_notification(
    payload: TestNotificationRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Queue an immediate test notification to a recipient."""
    await admin_service.send_test_notification(payload=payload, db=db)
    return {"status": "accepted", "message": "Test notification queued successfully"}
