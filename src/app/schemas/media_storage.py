"""Read-only storage observations; filesystem capacity and policy limits stay separate."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

StorageBytes = Annotated[int, Field(ge=0, strict=True)]


class MediaStorageStatus(BaseModel):
    status: Literal["ready", "unavailable"]
    storage_type: Literal["filesystem"]
    observation_started_at: AwareDatetime | None
    observed_at: AwareDatetime | None
    filesystem_total_bytes: StorageBytes | None
    filesystem_available_bytes: StorageBytes | None
    media_limit_bytes: StorageBytes | None
    daily_limit_bytes: StorageBytes | None
    daily_usage_bytes: StorageBytes | None
    daily_reserved_bytes: StorageBytes | None
    reason_code: (
        Literal["STORAGE_UNAVAILABLE", "STORAGE_SNAPSHOT_BUSY", "RESERVATION_SCOPE_UNKNOWN"] | None
    ) = None

    @field_validator("observation_started_at", "observed_at", mode="before")
    @classmethod
    def timestamp_type(cls, value: object) -> object:
        if value is not None and not isinstance(value, str | datetime):
            raise ValueError("Storage observation time must be an ISO8601 timestamp")
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("observation_started_at", "observed_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.utcoffset() != timedelta(0):
                raise ValueError("Storage observation time must use UTC")
            return value.astimezone(UTC)
        return None

    @model_validator(mode="after")
    def observation_scope(self) -> "MediaStorageStatus":
        observed = (
            self.observation_started_at,
            self.observed_at,
            self.filesystem_total_bytes,
            self.filesystem_available_bytes,
            self.daily_usage_bytes,
        )
        if self.status == "unavailable":
            if any(
                v is not None for v in (*observed, self.daily_reserved_bytes)
            ) or self.reason_code not in ("STORAGE_UNAVAILABLE", "STORAGE_SNAPSHOT_BUSY"):
                raise ValueError("Unavailable storage must not claim successful observations")
            return self
        if any(v is None for v in observed):
            raise ValueError("Ready storage requires the observed scope")
        assert self.observation_started_at is not None and self.observed_at is not None
        assert self.filesystem_total_bytes is not None
        assert self.filesystem_available_bytes is not None
        if (
            self.observed_at < self.observation_started_at
            or self.filesystem_available_bytes > self.filesystem_total_bytes
        ):
            raise ValueError("Storage observation values disagree")
        if self.daily_reserved_bytes is None:
            if self.reason_code != "RESERVATION_SCOPE_UNKNOWN":
                raise ValueError("Unknown reservation scope requires its reason")
        elif self.reason_code is not None:
            raise ValueError("Complete storage observations must not claim an unavailable scope")
        return self
