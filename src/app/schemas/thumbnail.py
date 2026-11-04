"""Dedicated thumbnail contract; legacy media-info remains unchanged."""

from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Reason = Literal[
    "NOT_PREPARED",
    "SOURCE_NOT_READY",
    "DAILY_MP4_REQUIRED",
    "SOURCE_UNAVAILABLE",
    "SOURCE_CHANGED",
    "IMAGE_CHANGED",
    "IMAGE_UNAVAILABLE",
    "NO_VIDEO_FRAME",
    "TIMELINE_UNAVAILABLE",
    "GENERATION_TIMEOUT",
    "GENERATION_FAILED",
    "INVALID_IMAGE",
    "IMAGE_TOO_LARGE",
    "STORAGE_UNAVAILABLE",
    "CAPACITY_EXCEEDED",
    "JOB_INTERRUPTED",
    "INSUFFICIENT_STORAGE",
]


class ThumbnailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_failed: bool = Field(default=False, strict=True)


class ThumbnailInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
    status: Literal["unsupported", "unavailable", "pending", "failed", "ready"]
    representative_at: AwareDatetime | None
    time_basis: Literal["server_received_at"] | None
    content_path: str | None = Field(max_length=256)
    width: int | None = Field(gt=0, le=640, strict=True)
    height: int | None = Field(gt=0, le=360, strict=True)
    size_bytes: int | None = Field(gt=0, le=524288, strict=True)
    reason_code: Reason | None
    generated_at: AwareDatetime | None

    @field_validator("representative_at", "generated_at", mode="before")
    @classmethod
    def utc_text(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("Thumbnail timestamps must be UTC ISO8601 strings")
        return value

    @field_validator("representative_at", "generated_at")
    @classmethod
    def utc_only(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("Thumbnail timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def consistent_state(self) -> "ThumbnailInfo":
        image = (self.content_path, self.width, self.height, self.size_bytes, self.generated_at)
        if self.status == "ready":
            if any(value is None for value in image):
                raise ValueError("Ready thumbnails require measured image properties")
            if self.representative_at is None:
                if self.time_basis is not None or self.reason_code != "TIMELINE_UNAVAILABLE":
                    raise ValueError("Unproven timeline must remain explicitly unavailable")
            elif self.time_basis != "server_received_at" or self.reason_code is not None:
                raise ValueError("A proven timeline requires its basis")
        elif any(value is not None for value in (*image, self.representative_at, self.time_basis)):
            raise ValueError("Non-ready thumbnails cannot claim an image or timeline")
        elif (self.reason_code is None) != (self.status in ("unsupported", "pending")):
            raise ValueError("Thumbnail status and reason disagree")
        return self


class ClipThumbnailInfo(ThumbnailInfo):
    clip_id: UUID


class DailyThumbnailInfo(ThumbnailInfo):
    recording_id: UUID
