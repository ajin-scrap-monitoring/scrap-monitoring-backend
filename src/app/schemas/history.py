"""History and operational event schemas conforming to frontend proposal."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.monitoring import LoadSample

EventCategory = Literal["collection", "alert", "error"]
HistoryMarkerType = Literal[
    "collection_required",
    "collection_completed",
    "measurement_error",
    "device_error",
    "video_delay",
]
EventType = Literal[
    "pre_collection_alert",
    "collection_required",
    "collection_started",
    "collection_completed",
    "measurement_error",
    "device_error",
    "video_delay",
]
EventStatus = Literal["active", "resolved", "completed"]


class CollectionThresholdSample(BaseModel):
    """Historical collection threshold valid at a given time."""

    model_config = ConfigDict(from_attributes=True)

    effectiveAt: datetime
    valuePercent: float = Field(ge=0.0, le=100.0)


class HistoryEventMarker(BaseModel):
    """Chart event marker displayed along the load timeline."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    type: HistoryMarkerType
    category: EventCategory
    severity: Literal["info", "warning", "error"]
    title: str
    detail: str
    occurredAt: datetime
    valuePercent: float = Field(ge=0.0, le=100.0)


class LoadHistory(BaseModel):
    """Representative load percentage history response."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    from_time: datetime = Field(alias="from")
    to_time: datetime = Field(alias="to")
    bucketSeconds: int = Field(ge=60)
    samples: list[LoadSample]
    collectionThresholds: list[CollectionThresholdSample]
    eventMarkers: list[HistoryEventMarker]


class Event(BaseModel):
    """Single operational event record."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    type: EventType
    category: EventCategory
    severity: Literal["info", "warning", "error"]
    status: EventStatus
    title: str
    detail: str
    occurredAt: datetime
    resolvedAt: datetime | None = None
    recordingId: str | None = None


class PageMetadata(BaseModel):
    """Pagination metadata conforming to proposal/openapi.yaml."""

    model_config = ConfigDict(from_attributes=True)

    page: int = Field(ge=1)
    pageSize: int = Field(ge=1)
    totalItems: int = Field(ge=0)
    totalPages: int = Field(ge=0)


class EventPage(BaseModel):
    """Paginated events list response."""

    model_config = ConfigDict(from_attributes=True)

    items: list[Event]
    page: PageMetadata
