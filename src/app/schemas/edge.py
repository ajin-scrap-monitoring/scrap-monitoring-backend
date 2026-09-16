"""Edge Platform measurement contract v1.0 and heartbeat schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EdgeQuality = Literal["GOOD", "DEGRADED", "INVALID"]


class EdgeSensorDetail(BaseModel):
    """Individual sensor diagnostic and measurement details."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    sequence: int
    instance: str
    height: float | None = None
    valid_sample_ratio: float | None = None
    coverage: float | None = None
    status: str = "OK"


class CameraSearchWindow(BaseModel):
    """Camera search window bounds."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    start_time: datetime | None = None
    end_time: datetime | None = None


class EdgeMetricIngestRequest(BaseModel):
    """Edge Platform measurement contract v1.0 payload."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    schema_version: str = Field(default="1.0", description="Contract schema version")
    measurement_id: str = Field(..., min_length=1, description="Unique measurement identifier")
    cycle_id: str | None = Field(default=None, description="Measurement cycle identifier")
    site_id: str | None = Field(default=None, description="Site identifier")
    edge_id: str | None = Field(default=None, description="Edge device identifier")
    measured_at: datetime = Field(..., description="Timestamp of measurement in UTC")

    calibration_version: str | None = Field(default=None, description="Calibration model version")
    config_revision: str | int | None = Field(default=None, description="Configuration revision")

    overall_quality: EdgeQuality = Field(
        default="GOOD",
        description="Overall measurement quality (GOOD, DEGRADED, INVALID)",
    )
    quality_reason_code: str | None = Field(
        default=None, description="Reason code for degraded/invalid"
    )

    fill_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Fill ratio (0.0 - 1.0)"
    )
    fill_percentage: float | None = Field(
        default=None, ge=0.0, le=100.0, description="Fill percentage (0.0 - 100.0)"
    )

    sensors: list[EdgeSensorDetail] = Field(
        default_factory=list, description="Sensor details array"
    )
    camera_search_window: CameraSearchWindow | None = Field(
        default=None, description="Camera search window"
    )


class EdgeMetricIngestResponse(BaseModel):
    """Acknowledgement response required by Edge Platform."""

    model_config = ConfigDict(from_attributes=True)

    measurement_id: str
    accepted: bool | None = None
    duplicate: bool | None = None


class EdgeHeartbeatRequest(BaseModel):
    """Edge device heartbeat report payload."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    schema_version: str = Field(default="1.0", description="Heartbeat contract version")
    site_id: str = Field(..., min_length=1, description="Site identifier")
    edge_id: str = Field(..., min_length=1, description="Edge device identifier")

    config_revision: str | int | None = None
    deployment_revision: str | int | None = None
    camera_id: str | None = None

    reported_at: datetime = Field(..., description="Timestamp of report in UTC")
    overall_status: str = Field(
        ..., description="Overall health status (HEALTHY, DEGRADED, UNHEALTHY, etc.)"
    )
    status_reason_code: str | None = None

    clock_sync_status: str | None = None
    clock_offset_ms: float | None = None

    services: Any = None


class EdgeHeartbeatResponse(BaseModel):
    """Heartbeat acknowledgement response."""

    model_config = ConfigDict(from_attributes=True)

    status: str = "ok"
    edge_id: str
    reported_at: datetime
