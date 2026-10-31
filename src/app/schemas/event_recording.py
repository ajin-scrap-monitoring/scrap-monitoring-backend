"""Event time references into existing daily HLS, never additional video files."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.history import PageMetadata

ReferenceCategory = Literal["collection", "alert", "error"]


class EventPlaybackSummary(BaseModel):
    state: Literal["AVAILABLE", "PARTIAL", "PROCESSING", "UNAVAILABLE", "UNKNOWN"]
    playbackSeconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reasonCodes: list[str] = Field(default_factory=list)


class EventRecording(BaseModel):
    eventId: str
    pitId: str
    category: ReferenceCategory
    title: str
    eventType: str
    occurredAt: datetime
    eventTo: datetime | None = None
    windowKind: Literal["COLLECTION_CYCLE", "EVENT_CONTEXT"]
    windowState: Literal["CLOSED", "OPEN", "UNKNOWN", "POINT"]
    windowFrom: datetime
    windowTo: datetime | None = None
    simulated: bool | None = None
    stateAtOccurrence: str | None = None
    pitAttribution: Literal["RECORDED", "CURRENT_ASSIGNMENT"] = "RECORDED"
    reasonCodes: list[str] = Field(default_factory=list)
    playback: EventPlaybackSummary | None = Field(
        default=None, description="Opt-in list summary; null when include_playback is false"
    )


class EventRecordingPage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: list[EventRecording]
    page: PageMetadata
    from_: datetime = Field(alias="from")
    to: datetime
    asOf: datetime

    snapshotToken: str | None = Field(default=None, exclude_if=lambda value: value is None)
    snapshotExpiresAt: datetime | None = Field(default=None, exclude_if=lambda value: value is None)


class ReplayPart(BaseModel):
    recordingId: UUID
    recordingDate: date
    playbackPath: str
    fromAt: datetime
    toAt: datetime
    startSeconds: float = Field(ge=0, allow_inf_nan=False)
    endSeconds: float = Field(gt=0, allow_inf_nan=False)
    relativeStartSeconds: float = Field(
        default=0,
        ge=0,
        allow_inf_nan=False,
        description="Start on the event's zero-based playable timeline, excluding missing spans",
    )
    relativeEndSeconds: float = Field(
        default=0,
        ge=0,
        allow_inf_nan=False,
        description="End on the event's zero-based playable timeline, excluding missing spans",
    )
    mapping: Literal["LINEAR", "POINT_TIMESTAMP"] = "LINEAR"


class UnavailableInterval(BaseModel):
    fromAt: datetime
    toAt: datetime
    kind: Literal["GAP", "UNCONFIRMED", "FUTURE", "MISSING", "EXPIRED"]
    reasonCode: str


class EventRecordingReplay(BaseModel):
    event: EventRecording
    source: Literal["DAILY_RECORDING"] = "DAILY_RECORDING"
    state: Literal["AVAILABLE", "PARTIAL", "PROCESSING", "UNAVAILABLE"]
    asOf: datetime
    effectiveTo: datetime | None = None
    requestedSeconds: float | None = None
    coveredSeconds: float = 0
    playbackSeconds: float = 0
    parts: list[ReplayPart] = Field(default_factory=list)
    unavailableIntervals: list[UnavailableInterval] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
    downloadPath: None = None
    downloadReasonCode: Literal["EVENT_RANGE_EXPORT_UNSUPPORTED"] = "EVENT_RANGE_EXPORT_UNSUPPORTED"
