"""Administrator history is separate from measurement, collection and alert events."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, field_validator

SettingsResource = Literal["alert_settings", "pit_config"]


class SettingsActor(BaseModel):
    id: str
    displayName: str


class SettingsHistoryItem(BaseModel):
    id: str
    occurredAt: AwareDatetime
    actor: SettingsActor
    pitId: str
    resource: SettingsResource
    operation: Literal["create", "update"]
    before: dict[str, Any] | None
    after: dict[str, Any]
    changedFields: list[str]

    @field_validator("occurredAt", mode="before")
    @classmethod
    def database_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class SettingsHistoryPage(BaseModel):
    items: list[SettingsHistoryItem]
    nextCursor: str | None
