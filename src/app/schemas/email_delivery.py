"""Administrative provider receipt; never exposes message bodies or credentials."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class EmailDeliveryReceipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    recipient_id: str
    pit_id: str
    phase: Literal["ALERT", "RECOVERY", "TEST"]
    status: Literal["QUEUED", "SENDING", "SENT", "FAILED", "UNCERTAIN", "CANCELLED"]
    due_at: datetime
    attempted_at: datetime | None
    completed_at: datetime | None
    attempts: int
    error_code: str | None

    @field_validator("due_at", "attempted_at", "completed_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
