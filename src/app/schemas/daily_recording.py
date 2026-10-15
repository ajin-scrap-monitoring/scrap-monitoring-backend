"""Camera v2 daily metadata. Media bodies remain on the authenticated gateway."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

DailyStatus = Literal["recording", "finalizing", "closed", "failed", "deleted", "expired"]


class DailyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    retention_days: int = Field(ge=1, le=365)


class DailyInterval(BaseModel):
    start_at: AwareDatetime
    end_at: AwareDatetime

    @field_validator("start_at", "end_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> "DailyInterval":
        # Deployed metadata can represent an interval with equal millisecond
        # timestamps. Preserve it and its playback mapping instead of inventing time.
        if self.start_at > self.end_at:
            raise ValueError("Recording interval cannot be reversed")
        return self


class DailyGap(DailyInterval):
    reason_code: str = Field(min_length=1, max_length=100)


class DailyAvailableInterval(DailyInterval):
    playback_start_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)
    playback_end_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def playback_ordered(self) -> "DailyAvailableInterval":
        if self.playback_start_seconds >= self.playback_end_seconds:
            raise ValueError("Playback interval must be nonempty")
        return self


class DailyError(BaseModel):
    code: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z0-9_]+$")
    message: str


class DailyRecording(BaseModel):
    """Do not equate a calendar window, recorded duration and export readiness."""

    recording_id: UUID
    camera_id: str
    recording_date: date
    timezone: Literal["Asia/Seoul"]
    window_start_at: AwareDatetime
    window_end_at: AwareDatetime
    status: DailyStatus
    coverage: Literal["complete", "partial", "none"] | None
    has_gaps: bool
    coverage_as_of_at: AwareDatetime
    playable_until_at: AwareDatetime | None
    recorded_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)
    gaps: list[DailyGap]
    available_intervals: list[DailyAvailableInterval]
    playback_state: Literal["pending", "ready", "unavailable"]
    playback_protocol: Literal["hls"] | None
    playback_path: str | None
    export_allowed: bool
    export_state: Literal["not_requested", "processing", "ready", "failed"]
    download_path: str | None
    size_bytes: int | None = Field(ge=0)
    expires_at: AwareDatetime | None
    export_expires_at: AwareDatetime | None
    error: DailyError | None

    @field_validator(
        "window_start_at",
        "window_end_at",
        "coverage_as_of_at",
        "playable_until_at",
        "expires_at",
        "export_expires_at",
    )
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_window(self) -> "DailyRecording":
        midnight = datetime.combine(self.recording_date, time(), ZoneInfo("Asia/Seoul"))
        if self.window_start_at != midnight or self.window_end_at != midnight + timedelta(days=1):
            raise ValueError("Expected a Korean calendar day")
        if not self.window_start_at <= self.coverage_as_of_at <= self.window_end_at:
            raise ValueError("Coverage is outside the recording window")
        if self.playable_until_at is not None and not (
            self.window_start_at <= self.playable_until_at <= self.coverage_as_of_at
        ):
            raise ValueError("Playback cannot extend beyond confirmed coverage")
        for intervals in (self.gaps, self.available_intervals):
            previous = self.window_start_at
            for interval in intervals:
                if interval.start_at < previous or interval.end_at > self.coverage_as_of_at:
                    raise ValueError("Intervals must be ordered within confirmed coverage")
                previous = interval.end_at
        return self


class DailyPage(BaseModel):
    items: list[DailyRecording]
    next_cursor: str | None
