"""Accepted JPEG receipt evidence is separate from camera hardware or image quality."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class CameraObservation(BaseModel):
    model_config = ConfigDict(strict=True)

    camera_id: str = Field(min_length=1, max_length=128)
    status_basis: Literal["accepted_jpeg_receipt"]
    status: Literal["healthy", "interrupted", "unknown"]
    connection_state: Literal["connected", "disconnected", "unknown"]
    last_frame_received_at: AwareDatetime | None
    assessed_at: AwareDatetime
    valid_until_at: AwareDatetime
    reason_code: Literal[
        "FRAME_RECEIVED",
        "NO_OBSERVATION",
        "AWAITING_FRAME",
        "FRAME_STALE",
        "FRAME_TIMEOUT",
        "CONNECTION_CLOSED",
        "CONNECTION_ERROR",
        "CANCELLED",
        "TEXT_FRAME_REJECTED",
        "INVALID_JPEG",
        "FRAME_TOO_LARGE",
    ]
    freshness_seconds: float = Field(gt=0, le=5)

    @field_validator("last_frame_received_at", "assessed_at", "valid_until_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        try:
            return value.astimezone(UTC) if value is not None else None
        except (OverflowError, ValueError):
            raise ValueError("Camera timestamp is outside UTC range") from None

    @model_validator(mode="after")
    def consistent(self) -> "CameraObservation":
        lease = (self.valid_until_at - self.assessed_at).total_seconds()
        if not 0 < lease <= self.freshness_seconds:
            raise ValueError("Invalid camera evidence lifetime")
        if self.status == "healthy":
            valid = (
                self.connection_state == "connected"
                and self.reason_code == "FRAME_RECEIVED"
                and self.last_frame_received_at is not None
            )
        elif self.status == "unknown":
            valid = self.last_frame_received_at is None and (
                self.connection_state,
                self.reason_code,
            ) in (("unknown", "NO_OBSERVATION"), ("connected", "AWAITING_FRAME"))
        elif self.reason_code in ("FRAME_STALE", "FRAME_TIMEOUT"):
            valid = self.connection_state == "connected" and (
                self.last_frame_received_at is not None
            ) == (self.reason_code == "FRAME_STALE")
        else:
            valid = self.connection_state == "disconnected" and self.reason_code not in (
                "FRAME_RECEIVED",
                "NO_OBSERVATION",
                "AWAITING_FRAME",
            )
        if not valid:
            raise ValueError("Contradictory camera evidence")
        return self


class CameraStatusV2(BaseModel):
    pitId: str
    cameraId: str | None
    state: Literal["HEALTHY", "INTERRUPTED", "UNKNOWN", "UNSUPPORTED"]
    connectionState: Literal["CONNECTED", "DISCONNECTED", "UNKNOWN"] = "UNKNOWN"
    statusBasis: Literal["ACCEPTED_JPEG_RECEIPT"] | None = None
    computedAt: datetime
    assessedAt: datetime | None = None
    validUntilAt: datetime | None = None
    validForSeconds: float | None = Field(default=None, ge=0, le=5)
    lastFrameReceivedAt: datetime | None = None
    reasonCodes: list[str]
