"""Media integration schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MediaRecordingCompleteRequest(BaseModel):
    """Payload received from Camera Media Service when recording segment completes."""

    segment_id: str = Field(min_length=1, max_length=100)
    pit_id: str = Field(default="pit-01", max_length=50)
    start_time: datetime
    end_time: datetime
    file_path: str = Field(min_length=1, max_length=255)
    file_size_bytes: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)


class MediaIndexResponse(BaseModel):
    """Media segment index response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    segment_id: str
    pit_id: str
    start_time: datetime
    end_time: datetime
    file_path: str
    file_size_bytes: int
    duration_seconds: float
    created_at: datetime


class MediaSessionVerifyResponse(BaseModel):
    """Media stream viewing permission check result."""

    authorized: bool
    stream_url: str | None = None
    message: str
