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
    durationSeconds: int = Field(ge=0)
    eventCategory: EventCategory
    title: str
    detail: str
    containerFormat: str = "mp4"
    videoCodec: str = "h264"
    widthPixels: int = Field(ge=1, default=640)
    heightPixels: int = Field(ge=1, default=480)
    frameRate: float = Field(gt=0.0, default=30.0)
    sizeBytes: int = Field(ge=0)
    retainedFrom: datetime
    retainedUntil: datetime
    status: RecordingStatus = "available"
    thumbnailPath: str | None = None
    contentPath: str | None = None
    downloadPath: str | None = None


class RecordingPage(BaseModel):
    """Paginated list of recorded video segments."""

    model_config = ConfigDict(from_attributes=True)

    items: list[Recording]
    page: PageMetadata
