"""Administration and alert settings endpoints conforming to proposal/openapi.yaml."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.admin_access import private_response, require_admin_change, require_administrator
from src.app.api.errors import error_responses
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
from src.app.schemas.session import Session
from src.app.schemas.settings_history import SettingsHistoryPage, SettingsResource
from src.app.services.admin_service import admin_service
from src.app.services.settings_history_service import history_page

admin_dependencies = [Depends(require_administrator), Depends(private_response)]
settings_router = APIRouter(
    prefix="/settings",
    tags=["Administration"],
    dependencies=admin_dependencies,
    responses=error_responses(401, 403, 422, 503),
)
recipients_router = APIRouter(
    prefix="/notification-recipients",
    tags=["Administration"],
    dependencies=admin_dependencies,
    responses=error_responses(401, 403, 422, 503),
)
notifications_admin_router = APIRouter(
    prefix="/notifications",
    tags=["Administration"],
    dependencies=admin_dependencies,
    responses=error_responses(401, 403, 422, 503),
)


@settings_router.get(
    "/alerts",
    response_model=AlertSettings,
    summary="Get alert settings",
)
async def get_alert_settings(
    response: Response,
    pit_id: str = Query(
        default="pit-01", min_length=1, max_length=50, description="Pit identifier"
    ),
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
    dependencies=[Depends(require_admin_change)],
    responses=error_responses(412, 428),
)
async def replace_alert_settings(
    payload: AlertSettingsUpdate,
    response: Response,
    pit_id: str = Query(
        default="pit-01", min_length=1, max_length=50, description="Pit identifier"
    ),
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db_session),
    session: Session = Depends(require_admin_change),
) -> AlertSettings:
    """Replace threshold and alert policy settings."""
    if if_match is None:
        raise HTTPException(428, "If-Match is required; retrieve the current settings first")
    updated, new_etag = await admin_service.replace_alert_settings(
        pit_id=pit_id, payload=payload, db=db, if_match=if_match, actor=session.user
    )
    response.headers["ETag"] = new_etag
    return updated


@settings_router.get("/history", response_model=SettingsHistoryPage)
async def get_settings_history(
    pit_id: str | None = Query(default=None, min_length=1, max_length=50),
    actor_id: str | None = Query(default=None, min_length=1, max_length=64),
    resource: SettingsResource | None = None,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, pattern=r"^[1-9][0-9]{0,18}$"),
    db: AsyncSession = Depends(get_db_session),
) -> SettingsHistoryPage:
    """Read committed changes newest first; this does not initialize settings."""
    if any(value is not None and value.tzinfo is None for value in (from_time, to_time)):
        raise HTTPException(422, "History times require a timezone")
    if from_time is not None:
        from_time = from_time.astimezone(UTC)
    if to_time is not None:
        to_time = to_time.astimezone(UTC)
    if from_time is not None and to_time is not None and from_time >= to_time:
        raise HTTPException(422, "from must precede to")
    if cursor is not None and int(cursor) > 9223372036854775807:
        raise HTTPException(422, "Invalid history cursor")
    return await history_page(
        db,
        limit=limit,
        cursor=int(cursor) if cursor else None,
        pit_id=pit_id,
        actor_id=actor_id,
        resource=resource,
        from_time=from_time,
        to_time=to_time,
    )


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
    dependencies=[Depends(require_admin_change)],
    responses=error_responses(409),
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
    dependencies=[Depends(require_admin_change)],
    responses=error_responses(404, 409, 412),
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
        recipient_id=recipient_id, payload=payload, db=db, if_match=if_match
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
    summary="Record a test notification request",
    dependencies=[Depends(require_admin_change)],
    responses=error_responses(404),
)
async def send_test_notification(
    payload: TestNotificationRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Queue an immediate test notification to a recipient.

    The current implementation records the request without external delivery.
    """
    await admin_service.send_test_notification(payload=payload, db=db)
    return {
        "status": "accepted",
        "message": "Test notification request recorded; no external delivery",
    }
