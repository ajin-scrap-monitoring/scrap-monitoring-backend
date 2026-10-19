"""Dashboard v2 contract: unknown values stay null and source judgments remain visible."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.edge_v2 import Assessment, DeviceStatus, Identifier
from src.app.schemas.fill_observation import FillObservation
from src.app.schemas.presentation import DisplayPercent

DashboardDataState = Literal["FRESH", "STALE", "UNAVAILABLE", "UNKNOWN", "NO_DATA"]
OperationalState = Literal["NORMAL", "WARNING", "CRITICAL"]


class MonitoringSource(BaseModel):
    """Explicit one-to-one pit/device assignment; never infer a pit from wire IDs."""

    model_config = ConfigDict(extra="forbid")

    pit_id: Identifier = Field(max_length=50)
    site_id: Identifier
    edge_id: Identifier


class MonitoringSummaryV2(BaseModel):
    simulated: bool = False
    inputSource: Literal["EDGE_STATE", "HEIGHT_SECTIONS"] = "EDGE_STATE"
    loadPercent: DisplayPercent | None = Field(default=None, ge=0, le=100)
    rawLoadPercent: DisplayPercent | None = Field(default=None, ge=0, le=100)
    filterState: Literal["DISABLED", "WARMING", "READY", "UNAVAILABLE"] = "DISABLED"
    warningThresholdPercent: float
    collectionThresholdPercent: float
    recoveryThresholdPercent: float


class OperationStatus(BaseModel):
    lastKnownState: OperationalState | None
    effectiveState: Literal["NORMAL", "WARNING", "CRITICAL", "UNKNOWN", "ERROR"]
    lastEvaluatedAt: datetime | None
    lastAlertAt: datetime | None
    notificationId: str | None
    evaluationSource: Literal["CURRENT_OBSERVATION", "RECENT_PERSISTED"] | None = None


class MonitoringSnapshotV2(BaseModel):
    schemaVersion: Literal["2"] = "2"
    snapshotId: str
    serverTime: datetime
    pitId: str
    source: MonitoringSource
    measuredAt: datetime | None
    assessmentReceivedAt: datetime | None
    dataState: DashboardDataState
    reasonCodes: list[str]
    summary: MonitoringSummaryV2
    assessment: Assessment | None
    deviceStatus: DeviceStatus
    operations: OperationStatus | None = None
    fillObservation: FillObservation | None = None


class CollectionEventPage(BaseModel):
    items: list[Assessment]
    totalItems: int
    limit: int
    offset: int


class LidarProfileV2(BaseModel):
    """No height is inferred from a reflection coordinate or an unverified floor distance.

    A verified, explicit frame/floor/region reference enables a separate height projection.
    """

    sensorId: str
    measuredAt: datetime | None = None
    configRevision: str | None = None
    calibrationVersion: str | None = None
    unit: Literal["cm"] = "cm"
    referenceDistanceCm: float
    simulated: bool = False
    timeBasis: Literal["SOURCE_UTC", "SIMULATION_SOURCE_UTC_UNVERIFIED"] = "SOURCE_UTC"
    referenceDepthCm: float | None = None
    sectionLengthCm: float | None = None
    calibrationState: Literal["UNVERIFIED", "VERIFIED", "UNAVAILABLE", "SIMULATION"] = "UNVERIFIED"
    average: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    # Angle samples stay current; only the representative scan mean is filtered in time.
    filteredAverage: float | None = None
    summaryValuePolicy: Literal["PROFILE_MEAN", "SCAN_MEAN_TEMPORAL_MEDIAN"] = "PROFILE_MEAN"
    samples: list[dict[str, float | int | bool | None]] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=lambda: ["HEIGHT_REFERENCE_UNVERIFIED"])
    coverageRatio: float = Field(default=0, ge=0, le=1)
    sectionFillPercent: DisplayPercent | None = Field(default=None, ge=0, le=100)
    coverageBasis: Literal["SPATIAL_BINS", "RECEIVED_POINTS"] = "SPATIAL_BINS"
    valuePolicy: Literal[
        "SPATIAL_BIN_MEDIAN",
        "SPATIAL_TEMPORAL_MEDIAN",
        "SENSOR_ANGLE",
    ] = "SPATIAL_BIN_MEDIAN"


class LidarProfilePageV2(BaseModel):
    pitId: str
    profiles: list[LidarProfileV2]
    # Equal weighting of two representative sections, not a validated pit volume estimate.
    meanSectionFillPercent: DisplayPercent | None = Field(default=None, ge=0, le=100)
    meanSectionReasonCodes: list[str] = Field(default_factory=list)


class StateEventV2(BaseModel):
    id: int
    occurredAt: datetime
    kind: str
    detail: dict[str, Any]


class StateEventPageV2(BaseModel):
    items: list[StateEventV2]
    totalItems: int
    limit: int
    offset: int


class V2Alert(BaseModel):
    simulated: bool = False
    eventId: str
    notificationId: str
    assessmentId: str | None
    observationId: str | None = None
    inputSource: Literal["EDGE_STATE", "HEIGHT_SECTIONS"] = "EDGE_STATE"
    loadPercent: DisplayPercent
    observedAt: datetime
    triggeredAt: datetime
    readAt: datetime | None


class V2AlertPage(BaseModel):
    items: list[V2Alert]
    totalItems: int
    limit: int
    offset: int
