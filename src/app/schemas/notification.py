"""Notification center schemas conforming to proposal/openapi.yaml."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.history import EventType, PageMetadata

NotificationSeverity = Literal["info", "warning", "error"]


class Notification(BaseModel):
    """User notification item for top header notification center."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Unique notification identifier", examples=["notif-001"])
    type: EventType = Field(..., description="Operational event type")
    severity: NotificationSeverity = Field(..., description="Alert severity level")
    title: str = Field(..., description="Short summary title")
    detail: str = Field(..., description="Detailed message or context")
    occurredAt: datetime = Field(..., description="Timestamp when the notification occurred")
    readAt: datetime | None = Field(default=None, description="Timestamp when marked as read")


class NotificationUpdate(BaseModel):
    """Payload to mark a notification as read."""

    model_config = ConfigDict(from_attributes=True)

    read: Literal[True] = Field(..., description="Mark notification as read")


class NotificationPage(BaseModel):
    """Paginated list of notifications with unread count."""

    model_config = ConfigDict(from_attributes=True)

    items: list[Notification]
    page: PageMetadata
    unreadCount: int = Field(ge=0, description="Total unread notifications count")
