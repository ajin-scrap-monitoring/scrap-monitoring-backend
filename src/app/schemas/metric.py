"""Metric Ingestion and Query Pydantic Schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MetricIngestRequest(BaseModel):
    """Payload sent by LiDAR Edge processing service."""

    pit_id: str = Field(default="pit-01", max_length=50, description="Target scrap pit ID")
    measured_at: datetime = Field(description="Timestamp of measurement in UTC")
    lidar1_distance_cm: float | None = Field(
        default=None,
        description="Distance from LiDAR 1 to scrap surface in cm",
    )
    lidar2_distance_cm: float | None = Field(
        default=None,
        description="Distance from LiDAR 2 to scrap surface in cm",
    )
    calculated_height_cm: float = Field(
        ge=0.0,
        description="Representative scrap accumulation height in cm",
    )
    fill_ratio_percent: float = Field(
        ge=0.0,
        le=100.0,
        description="Estimated scrap fill ratio in percentage (0.0 - 100.0)",
    )
    sensor1_status: str = Field(default="OK", max_length=20, description="Status of LiDAR 1")
    sensor2_status: str = Field(default="OK", max_length=20, description="Status of LiDAR 2")


class MetricResponse(BaseModel):
    """Scrap metric response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    pit_id: str
    measured_at: datetime
    lidar1_distance_cm: float | None
    lidar2_distance_cm: float | None
    calculated_height_cm: float
    fill_ratio_percent: float
    state: str
    sensor1_status: str
    sensor2_status: str
    is_valid: bool
    created_at: datetime


class MetricStatsSummary(BaseModel):
    """Aggregated time-series statistical point."""

    timestamp: datetime
    avg_fill_ratio: float
    max_fill_ratio: float
    min_fill_ratio: float
    avg_height_cm: float
    sample_count: int
