"""Pit configuration schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PitConfigResponse(BaseModel):
    """Configuration response for a scrap pit."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    total_depth_cm: float
    warning_threshold_percent: float
    critical_threshold_percent: float
    updated_at: datetime


class PitConfigUpdateRequest(BaseModel):
    """Update request for scrap pit configuration."""

    name: str | None = Field(default=None, max_length=100)
    total_depth_cm: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    warning_threshold_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    critical_threshold_percent: float | None = Field(default=None, ge=0.0, le=100.0)
