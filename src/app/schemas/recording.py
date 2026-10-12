"""Recording metadata schemas conforming to frontend dashboard proposal."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.history import EventCategory, PageMetadata

RecordingStatus = Literal["available", "processing", "expired", "unavailable"]


class Recording(BaseModel):
    """Metadata for a single recorded video segment."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    startedAt: datetime
    endedAt: datetime
    durationSeconds: int | None = Field(ge=0)
    eventCategory: EventCategory
    title: str
    detail: str
    containerFormat: str = "mp4"
    videoCodec: str = "h264"
    widthPixels: int | None = Field(ge=1, default=640)
    heightPixels: int | None = Field(ge=1, default=480)
    frameRate: float | None = Field(gt=0.0, default=30.0)
    sizeBytes: int | None = Field(ge=0)
    retainedFrom: datetime | None
    retainedUntil: datetime | None
    status: RecordingStatus = "available"
    thumbnailPath: str | None = None
    contentPath: str | None = None
    downloadPath: str | None = None
    # Media v1 does not report dimensions, FPS, thumbnails or file sizes.
    mediaStatus: str | None = None
    coverage: str | None = None
    gaps: list[dict[str, datetime]] = Field(default_factory=list)
    errorCode: str | None = None


class RecordingPage(BaseModel):
    """Paginated list of recorded video segments."""

    model_config = ConfigDict(from_attributes=True)

    items: list[Recording]
    page: PageMetadata
