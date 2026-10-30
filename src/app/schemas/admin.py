"""Administration and alert settings schemas conforming to proposal/openapi.yaml."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from src.app.core.alert_thresholds import validate_alert_thresholds
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
    sendDelayMinutes: int = Field(ge=0, le=2147483647, default=0)
    repeatIntervalMinutes: int = Field(ge=0, le=2147483647, default=30)
    maximumRepeatCount: int = Field(ge=1, le=2147483647, default=3)
    recoveryNotificationEnabled: bool = True
    eventTypes: list[ConfigurableAlertEventType] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        """Reject settings that the existing operational evaluator cannot use."""
        try:
            validate_alert_thresholds(
                self.preCollectionAlert.thresholdPercent, self.collectionThresholdPercent
            )
        except ValueError:
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [
                    {
                        "type": PydanticCustomError(
                            "alert_threshold_gap",
                            "Pre-alert threshold must be at least {minimumGap} percentage points "
                            "below collection threshold",
                            {
                                "minimumGap": 5,
                                "collectionThresholdPercent": self.collectionThresholdPercent,
                            },
                        ),
                        "loc": ("preCollectionAlert", "thresholdPercent"),
                        "input": self.preCollectionAlert.thresholdPercent,
                    }
                ],
            ) from None
        return self


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

    name: str = Field(min_length=1, max_length=100)
    team: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, max_length=50)
    enabled: bool = True
    channels: list[NotificationChannel] = Field(min_length=1, max_length=2)
    subscriptionMode: Literal["global", "custom"] = "global"
    eventTypes: list[ConfigurableAlertEventType] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_selections(self) -> Self:
        """Keep stored channel and event lists unique, as recipient updates require."""
        for field in ("channels", "eventTypes"):
            values = getattr(self, field)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must contain unique values")
        return self


class NotificationRecipientPatch(BaseModel):
    """Payload for partially updating a notification recipient."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    team: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, max_length=50)
    enabled: bool | None = None
    channels: list[NotificationChannel] | None = Field(default=None, min_length=1, max_length=2)
    subscriptionMode: Literal["global", "custom"] | None = None
    eventTypes: list[ConfigurableAlertEventType] | None = Field(default=None, max_length=3)

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        """Omitted values stay unchanged; only the optional phone can be cleared."""
        if not self.model_fields_set:
            raise ValueError("At least one recipient field is required")
        for field in self.model_fields_set - {"phone"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        for field in ("channels", "eventTypes"):
            values = getattr(self, field)
            if values is not None and len(values) != len(set(values)):
                raise ValueError(f"{field} must contain unique values")
        return self


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
