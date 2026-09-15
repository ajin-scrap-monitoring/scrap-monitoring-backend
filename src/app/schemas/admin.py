"""Administration and alert settings schemas conforming to proposal/openapi.yaml."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.history import PageMetadata
from src.app.schemas.monitoring import PreCollectionAlert

ConfigurableAlertEventType = Literal["collection_required", "measurement_error", "device_error"]
NotificationChannel = Literal["email", "sms"]
SubscriptionMode = Literal["global", "custom"]


class AlertSettings(BaseModel):
    """Current alert policy and threshold settings."""

    model_config = ConfigDict(from_attributes=True)

    version: str = Field(pattern=r'^"[^"]+"$')
    collectionThresholdPercent: float = Field(ge=0.0, le=100.0)
    preCollectionAlert: PreCollectionAlert
    sendDelayMinutes: int = Field(ge=0, default=0)
    repeatIntervalMinutes: int = Field(ge=0, default=30)
    maximumRepeatCount: int = Field(ge=1, default=3)
    recoveryNotificationEnabled: bool = True
    eventTypes: list[ConfigurableAlertEventType] = Field(
        default=["collection_required", "measurement_error", "device_error"]
    )


class AlertSettingsUpdate(BaseModel):
    """Payload for updating alert policy and threshold settings."""

    model_config = ConfigDict(from_attributes=True)

    collectionThresholdPercent: float = Field(ge=0.0, le=100.0)
    preCollectionAlert: PreCollectionAlert
    sendDelayMinutes: int = Field(ge=0, default=0)
    repeatIntervalMinutes: int = Field(ge=0, default=30)
    maximumRepeatCount: int = Field(ge=1, default=3)
    recoveryNotificationEnabled: bool = True
    eventTypes: list[ConfigurableAlertEventType] = Field(min_length=1)


class NotificationRecipient(BaseModel):
    """Registered notification recipient detail."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    version: str = Field(pattern=r'^"[^"]+"$')
    name: str
    team: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = None
    enabled: bool = True
    channels: list[NotificationChannel]
    subscriptionMode: Literal["global", "custom"] = "global"
    eventTypes: list[ConfigurableAlertEventType]


class NotificationRecipientCreate(BaseModel):
    """Payload for creating a new notification recipient."""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1)
    team: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = None
    enabled: bool = True
    channels: list[NotificationChannel] = Field(min_length=1)
    subscriptionMode: Literal["global", "custom"] = "global"
    eventTypes: list[ConfigurableAlertEventType] = Field(min_length=1)


class NotificationRecipientPatch(BaseModel):
    """Payload for partially updating a notification recipient."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = None
    team: str | None = None
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = None
    enabled: bool | None = None
    channels: list[NotificationChannel] | None = None
    subscriptionMode: Literal["global", "custom"] | None = None
    eventTypes: list[ConfigurableAlertEventType] | None = None


class NotificationRecipientPage(BaseModel):
    """Paginated list of notification recipients."""

    model_config = ConfigDict(from_attributes=True)

    items: list[NotificationRecipient]
    page: PageMetadata


class TestNotificationRequest(BaseModel):
    """Payload for requesting an immediate test notification."""

    model_config = ConfigDict(from_attributes=True)

    recipientId: str = Field(min_length=1)
    channels: list[NotificationChannel] = Field(min_length=1)
