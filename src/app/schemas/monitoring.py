"""Monitoring snapshot and realtime contract schemas conforming to frontend proposal."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SystemStatus = Literal[
    "normal", "collection_required", "measurement_error", "disconnected", "no_data"
]
OperationState = Literal["idle", "accumulating", "collecting"]
DeviceType = Literal["lidar", "camera", "edge"]
DeviceHealthStatus = Literal["normal", "delayed", "unavailable"]
AlertSeverity = Literal["info", "warning", "error"]
EventType = Literal[
    "pre_collection_alert",
    "collection_required",
    "collection_started",
    "collection_completed",
    "measurement_error",
    "device_error",
    "video_delay",
]


class PreCollectionAlert(BaseModel):
    """Pre-collection alert threshold config."""

    model_config = ConfigDict(from_attributes=True)

    enabled: bool = True
    thresholdPercent: float = Field(ge=0.0, le=100.0)


class MonitoringSummary(BaseModel):
    """High-level summary for the dashboard header."""

    model_config = ConfigDict(from_attributes=True)

    loadPercent: float = Field(ge=0.0, le=100.0)
    collectionThresholdPercent: float = Field(ge=0.0, le=100.0)
    preCollectionAlert: PreCollectionAlert
    loadChangeLastHourPercentagePoints: float = Field(ge=-100.0, le=100.0, default=0.0)
    lastCollectionAt: datetime | None = None
    averageCollectionCycleSeconds: int | None = None
    expectedThresholdAt: datetime | None = None


class DeviceStatus(BaseModel):
    """Individual hardware/edge device status item."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    type: DeviceType
    displayName: str
    status: DeviceHealthStatus
    lastReceivedAt: datetime | None = None
    latencyMilliseconds: int | None = None


class Alert(BaseModel):
    """Active alert item displayed on dashboard."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    type: EventType
    severity: AlertSeverity
    title: str
    detail: str
    occurredAt: datetime


class LoadSample(BaseModel):
    """Time-series load sample for the trend sparkline/chart."""

    model_config = ConfigDict(from_attributes=True)

    measuredAt: datetime
    valuePercent: float | None = None
    valid: bool = True


class LidarProfileSample(BaseModel):
    """Single surface measurement point along the LiDAR scan line."""

    model_config = ConfigDict(from_attributes=True)

    positionRatio: float = Field(ge=0.0, le=1.0)
    height: float | None = None
    valid: bool = True


class LidarProfile(BaseModel):
    """LiDAR surface cross-section profile."""

    model_config = ConfigDict(from_attributes=True)

    lidarId: Literal["lidar-1", "lidar-2"]
    measuredAt: datetime
    unit: Literal["m"] = "m"
    average: float = Field(ge=0.0)
    minimum: float = Field(ge=0.0)
    maximum: float = Field(ge=0.0)
    samples: list[LidarProfileSample]


class LiveVideo(BaseModel):
    """Live video camera status metadata."""

    model_config = ConfigDict(from_attributes=True)

    streamId: str = "camera-main"
    capturedAt: datetime
    status: Literal["available", "delayed", "unavailable"] = "available"


class MonitoringSnapshot(BaseModel):
    """Complete dashboard state snapshot conforming to proposal/openapi.yaml."""

    model_config = ConfigDict(from_attributes=True)

    schemaVersion: str = "1"
    snapshotId: str
    serverTime: datetime
    measuredAt: datetime
    systemStatus: SystemStatus
    operationState: OperationState
    summary: MonitoringSummary
    devices: list[DeviceStatus]
    activeAlerts: list[Alert]
    recentLoad: list[LoadSample]
    lidarProfiles: list[LidarProfile]
    video: LiveVideo
