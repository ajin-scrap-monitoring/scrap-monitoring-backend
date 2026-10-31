"""Actual file measurements; never substitute capture clocks or recording settings."""

import math
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class VideoFormat(BaseModel):
    codec: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,64}$")
    width: int | None = Field(default=None, gt=0, strict=True)
    height: int | None = Field(default=None, gt=0, strict=True)
    fps: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    fps_num: int | None = Field(default=None, gt=0, strict=True)
    fps_den: int | None = Field(default=None, gt=0, strict=True)

    def complete_format(self) -> bool:
        return all(
            value is not None
            for value in (self.codec, self.width, self.height, self.fps, self.fps_num, self.fps_den)
        )

    @model_validator(mode="after")
    def consistent_rate(self) -> "VideoFormat":
        if self.fps is not None and self.fps_num is not None and self.fps_den is not None:
            try:
                expected = self.fps_num / self.fps_den
            except OverflowError as exc:
                raise ValueError("Nominal frame rate is out of range") from exc
            if not math.isclose(self.fps, expected, rel_tol=1e-9):
                raise ValueError("Nominal rate must match its rational representation")
        return self


class FileMediaInfo(VideoFormat):
    status: Literal["ready", "unknown"]
    source: Literal["file_probe"]
    size_bytes: int | None = Field(ge=0, strict=True)
    reason_code: (
        Literal[
            "file_unavailable",
            "probe_failed",
            "probe_timeout",
            "probe_unavailable",
            "file_changed",
            "metadata_unavailable",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def measured_or_unknown(self) -> "FileMediaInfo":
        if self.status == "ready":
            if (
                not self.complete_format()
                or self.size_bytes is None
                or self.reason_code is not None
            ):
                raise ValueError("Ready file information requires actual measurements")
        elif (
            any(
                value is not None
                for value in (
                    self.codec,
                    self.width,
                    self.height,
                    self.fps,
                    self.fps_num,
                    self.fps_den,
                )
            )
            or self.reason_code is None
        ):
            raise ValueError("Unknown file information must not claim measured format values")
        return self


class SegmentFormat(VideoFormat):
    segment_count: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def measured_format(self) -> "SegmentFormat":
        if not self.complete_format():
            raise ValueError("A measured segment format must include all properties")
        return self


class SegmentMediaInfo(BaseModel):
    status: Literal["ready", "partial", "unknown"]
    total: int = Field(ge=0, strict=True)
    measured: int = Field(ge=0, strict=True)
    mixed: bool | None = Field(strict=True)
    formats: list[SegmentFormat]

    @model_validator(mode="after")
    def measured_scope(self) -> "SegmentMediaInfo":
        if (
            self.measured > self.total
            or sum(x.segment_count for x in self.formats) != self.measured
        ):
            raise ValueError("Measured segment counts must agree")
        keys = {(x.codec, x.width, x.height, x.fps_num, x.fps_den) for x in self.formats}
        if len(keys) != len(self.formats):
            raise ValueError("Repeated formats cannot establish mixed recording formats")
        status = (
            "ready"
            if self.measured == self.total and self.measured
            else "partial"
            if self.measured
            else "unknown"
        )
        mixed = (
            True
            if len(self.formats) > 1
            else False
            if self.measured == self.total and self.measured
            else None
        )
        if self.status != status or self.mixed is not mixed:
            raise ValueError("Segment status must describe the actual measured scope")
        return self


class RecordingTimestampInfo(BaseModel):
    time_basis: Literal["server_received_at"]
    capture_timestamp_supported: Literal[False]
    capture_timestamp: None
    capture_clock_source: None
    clock_synchronization: Literal["unavailable"]


class RecordingThumbnailInfo(BaseModel):
    status: Literal["unsupported"]
    representative_at: None
    content_path: None


class ClipMediaInfo(BaseModel):
    clip_id: UUID
    size_bytes: int | None = Field(ge=0, strict=True)
    media_info: FileMediaInfo
    timestamp_info: RecordingTimestampInfo
    thumbnail: RecordingThumbnailInfo

    @model_validator(mode="after")
    def consistent_size(self) -> "ClipMediaInfo":
        if self.size_bytes != self.media_info.size_bytes:
            raise ValueError("File size must match the file measurement")
        return self


class DailyMediaInfo(BaseModel):
    recording_id: UUID
    file: FileMediaInfo | None
    segments: SegmentMediaInfo
    timestamp_info: RecordingTimestampInfo
    thumbnail: RecordingThumbnailInfo
