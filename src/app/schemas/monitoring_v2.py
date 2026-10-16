"""Dashboard v2 contract: unknown values stay null and source judgments remain visible."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.edge_v2 import Assessment, DeviceStatus, Identifier
from src.app.schemas.fill_observation import FillObservation

DashboardDataState = Literal["FRESH", "STALE", "UNAVAILABLE", "UNKNOWN", "NO_DATA"]
OperationalState = Literal["NORMAL", "WARNING", "CRITICAL"]


class MonitoringSource(BaseModel):
    """Explicit one-to-one pit/device assignment; never infer a pit from wire IDs."""

    model_config = ConfigDict(extra="forbid")

    pit_id: Identifier = Field(max_length=50)
    site_id: Identifier
    edge_id: Identifier


class MonitoringSummaryV2(BaseModel):
    inputSource: Literal["EDGE_STATE", "HEIGHT_SECTIONS"] = "EDGE_STATE"
    loadPercent: float | None = Field(default=None, ge=0, le=100)
    rawLoadPercent: float | None = Field(default=None, ge=0, le=100)
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
    calibrationState: Literal["UNVERIFIED", "VERIFIED", "UNAVAILABLE"] = "UNVERIFIED"
    average: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    samples: list[dict[str, float | bool | None]] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=lambda: ["HEIGHT_REFERENCE_UNVERIFIED"])
    coverageRatio: float = Field(default=0, ge=0, le=1)
    sectionFillPercent: float | None = Field(default=None, ge=0, le=100)
    valuePolicy: Literal["SPATIAL_BIN_MEDIAN"] = "SPATIAL_BIN_MEDIAN"


class LidarProfilePageV2(BaseModel):
    pitId: str
    profiles: list[LidarProfileV2]
    # Equal weighting of two representative sections, not a validated pit volume estimate.
    meanSectionFillPercent: float | None = Field(default=None, ge=0, le=100)
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
    eventId: str
    notificationId: str
    assessmentId: str | None
    observationId: str | None = None
    inputSource: Literal["EDGE_STATE", "HEIGHT_SECTIONS"] = "EDGE_STATE"
    loadPercent: float
    observedAt: datetime
    triggeredAt: datetime
    readAt: datetime | None


class V2AlertPage(BaseModel):
    items: list[V2Alert]
    totalItems: int
    limit: int
    offset: int
