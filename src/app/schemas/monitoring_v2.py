"""Dashboard v2 contract: unknown values stay null and source judgments remain visible."""

from datetime import datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.active_alerts_v2 import ActiveFillAlertPageV2
from src.app.schemas.alert_lifecycle import AlertLifecycle
from src.app.schemas.edge_v2 import Assessment, DeviceStatus, Identifier
from src.app.schemas.fill_observation import FillObservation
from src.app.schemas.presentation import DisplayPercent
from src.app.schemas.response_fields import RequiredResponseFields

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


class MonitoringSnapshotWithAlertsV2(BaseModel):
    """One current snapshot and the active alarms derived from that exact snapshot."""

    snapshot: MonitoringSnapshotV2
    activeAlerts: ActiveFillAlertPageV2


class CollectionEventPage(BaseModel):
    items: list[Assessment]
    totalItems: int
    limit: int
    offset: int


class LidarProfileSampleV2(TypedDict):
    """One received or spatially grouped point; unavailable positions remain null.

    Angle metadata is present only for SENSOR_ANGLE samples. Keep dictionary semantics
    for the projection/filter pipeline and omit absent keys from existing responses.
    """

    positionRatio: Annotated[float | None, Field(ge=0, le=1)]
    height: Annotated[float | None, Field(ge=0, description="Height in cm")]
    valid: bool
    angleMdeg: NotRequired[int]
    sourceIndex: NotRequired[int]
    binSource: NotRequired[Literal["OBSERVED", "INTERPOLATED", "UNAVAILABLE", "MASKED"]]


class LidarProfileV2(RequiredResponseFields):
    """No height is inferred from a reflection coordinate or an unverified floor distance.

    A verified, explicit frame/floor/region reference enables a separate height projection.
    """

    # Require only the emitted array; nested optional sample fields stay optional.
    required_response_fields = ("samples",)

    sensorId: str
    measuredAt: datetime | None = None
    configRevision: str | None = None
    calibrationVersion: str | None = None
    # Legacy clients accept omission but reject unknown enum values. Section input
    # uses its own optional provenance field instead of mislabelling its source.
    sampleSource: Literal["MEASUREMENT", "FILTERED_POINT_CLOUD"] | None = Field(
        default="MEASUREMENT", exclude_if=lambda value: value is None
    )
    sectionSource: Literal["EDGE_SECTION_PROFILE"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    sectionJudgmentState: Literal["AVAILABLE", "LIMITED", "UNAVAILABLE"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    processingVersion: str | None = None
    sourceScanId: str | None = None
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
    # Source angle samples stay current; only the scan mean is filtered in time.
    # A derived spatial display does not replace this representative statistic.
    filteredAverage: float | None = None
    summaryValuePolicy: Literal["PROFILE_MEAN", "SCAN_MEAN_TEMPORAL_MEDIAN"] = "PROFILE_MEAN"
    samples: list[LidarProfileSampleV2] = Field(default_factory=list)
    # Presentation metadata is independent of received-point statistics and windows.
    displayBinCount: int | None = Field(default=None, ge=2, le=200, exclude_if=lambda v: v is None)
    displayCoverageRatio: float | None = Field(
        default=None, ge=0, le=1, exclude_if=lambda v: v is None
    )
    displayUnpositionedCount: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
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
    # Optional current fusion result; meanSectionFillPercent keeps the selected
    # five-second display input. LIMITED raw values never approve an alert.
    rawMeanSectionFillPercent: DisplayPercent | None = Field(
        default=None, ge=0, le=100, exclude_if=lambda value: value is None
    )


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
    lifecycle: AlertLifecycle = Field(default_factory=AlertLifecycle)
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
