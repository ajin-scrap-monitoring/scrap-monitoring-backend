"""Explicit event downloads, separate from read-only archive playback."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.media_service import ClipCoverage, ClipGap


class EventExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestId: UUID
    beforeSeconds: int = Field(default=30, ge=0, le=300, strict=True)
    afterSeconds: int = Field(default=30, ge=0, le=300, strict=True)
    retryFailed: bool = Field(default=False, strict=True)


class EventExportResponse(BaseModel):
    requestId: UUID
    eventId: str
    status: str
    requestedFrom: datetime
    requestedTo: datetime
    statusPath: str
    downloadPath: str | None = None
    clipId: UUID | None = None
    coverage: ClipCoverage | None = None
    actualFrom: datetime | None = None
    actualTo: datetime | None = None
    availableIntervals: list[ClipGap] = Field(default_factory=list)
    gaps: list[ClipGap] = Field(default_factory=list)
    extractionMode: Literal["stream_copy"] | None = None
    sizeBytes: int | None = None
    expiresAt: datetime | None = None
    attemptCount: int | None = None
    errorCode: str | None = None
