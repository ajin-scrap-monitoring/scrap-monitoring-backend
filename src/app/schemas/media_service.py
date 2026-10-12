"""Camera media service v1 JSON contract; video bytes remain outside FastAPI."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ClipStatus = Literal["collecting", "processing", "ready", "failed", "deleted", "expired"]
ClipCoverage = Literal["complete", "partial", "none"]


class RecordingPolicy(BaseModel):
    """Policy applies only to events first accepted after the update."""

    model_config = ConfigDict(extra="forbid", strict=True)

    pre_roll_seconds: int = Field(ge=0, le=300)
    post_roll_seconds: int = Field(ge=0, le=300)
    retention_days: int = Field(ge=1, le=365)

    @model_validator(mode="after")
    def validate_duration(self) -> "RecordingPolicy":
        if self.pre_roll_seconds + self.post_roll_seconds < 1:
            raise ValueError("The total recording window must be at least one second")
        return self


class ClipRequest(BaseModel):
    """Stable backend event identity, reused on every retry."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    camera_id: str = Field(default="camera-main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")

    @field_validator("event_id")
    @classmethod
    def nonblank_event(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event_id cannot be blank")
        return value


class ClipReceipt(BaseModel):
    """Creation receipt is not confirmation that a playable file exists."""

    clip_id: UUID
    event_id: str
    camera_id: str
    status: ClipStatus
    accepted_at: AwareDatetime
    requested_start_at: AwareDatetime
    requested_end_at: AwareDatetime

    @field_validator("accepted_at", "requested_start_at", "requested_end_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self) -> "ClipReceipt":
        if self.requested_start_at >= self.requested_end_at:
            raise ValueError("The requested recording interval must be nonempty")
        return self


class ClipGap(BaseModel):
    start_at: AwareDatetime
    end_at: AwareDatetime

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ClipError(BaseModel):
    code: str
    message: str


class ClipDetail(ClipReceipt):
    """Full remote state, including partial coverage and nullable completion times."""

    coverage: ClipCoverage | None
    recording_policy: RecordingPolicy
    actual_start_at: AwareDatetime | None
    actual_end_at: AwareDatetime | None
    gaps: list[ClipGap]
    completed_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    deleted_at: AwareDatetime | None
    content_path: str | None
    error: ClipError | None

    @field_validator("actual_start_at", "actual_end_at", "completed_at", "expires_at", "deleted_at")
    @classmethod
    def normalize_optional_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_content_path(self) -> "ClipDetail":
        expected = f"/api/v1/clips/{self.clip_id}/content"
        if self.content_path is not None and self.content_path != expected:
            raise ValueError("Unexpected media content path")
        if self.status != "ready" and self.content_path is not None:
            raise ValueError("Only ready clips may expose content")
        if (
            self.actual_start_at
            and self.actual_end_at
            and self.actual_start_at > self.actual_end_at
        ):
            raise ValueError("The actual recording interval cannot be reversed")
        if self.status == "ready" and (
            not self.content_path
            or not self.completed_at
            or not self.expires_at
            or not self.actual_start_at
            or not self.actual_end_at
            or self.coverage not in ("complete", "partial")
        ):
            raise ValueError("Ready clips require content, timestamps and coverage")
        return self


class ClipPage(BaseModel):
    items: list[ClipDetail]
    next_cursor: str | None


class ClipJobResponse(BaseModel):
    """Durable local dispatch state; the remote snapshot is absent before acceptance."""

    event_id: str
    camera_id: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    error_code: str | None
    clip: ClipReceipt | ClipDetail | None
