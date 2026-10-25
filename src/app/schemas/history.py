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
EventStatus = Literal["active", "resolved", "completed", "unknown"]
CollectionScope = Literal["current", "all"]


class CollectionCriteria(BaseModel):
    """Only retained original criteria; absent historic fields are never reconstructed."""

    scopeToken: str | None = None
    matchesCurrentScope: bool | None = None
    detectionVersion: str | None = None
    heightBasis: str | None = None
    detectionProfile: str | None = None
    settleSeconds: int | None = Field(default=None, gt=0)
    endPolicy: str | None = None


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
    resolutionObservedAt: datetime | None = None
    recordingId: str | None = None
    collectionSource: Literal["MANUAL_CONFIRMATION", "HEIGHT_SETTLED"] | None = None
    confirmedCompletion: bool | None = None
    simulated: bool | None = None
    detectionId: str | None = None
    collectionCriteria: CollectionCriteria | None = None
    eventSource: Literal["EDGE_V2_TRANSITION"] | None = None
    transitionId: int | None = None
    transitionKind: Literal["DEVICE_STATE_CHANGED", "MEASUREMENT_VALIDITY_CHANGED"] | None = None
    stateAtOccurrence: (
        Literal["UNAVAILABLE", "RECOVERED", "PREPARING", "DEGRADED", "UNKNOWN"] | None
    ) = None
    statusBasis: (
        Literal["AT_OCCURRENCE", "CURRENT_EVALUATION", "PERSISTED_RESOLUTION", "UNVERIFIED"] | None
    ) = None
    pitAttribution: Literal["RECORDED", "CURRENT_ASSIGNMENT"] | None = None
    reasonCodes: list[str] = Field(default_factory=list)


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
