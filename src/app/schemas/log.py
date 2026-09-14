"""Operational and collection audit log schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AlertLogResponse(BaseModel):
    """Alert event log response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    pit_id: str
    triggered_at: datetime
    fill_ratio_percent: float
    channel: str
    status: str
    message: str
    created_at: datetime


class CollectionConfirmRequest(BaseModel):
    """Payload to confirm scrap collection completion."""

    pit_id: str = Field(default="pit-01")
    residual_height_cm: float = Field(
        ge=0.0, description="Measured scrap height remaining after haul"
    )
    operator_name: str = Field(min_length=1, max_length=100)
    notes: str | None = None


class CollectionLogResponse(BaseModel):
    """Collection event log response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    pit_id: str
    requested_at: datetime | None
    completed_at: datetime
    residual_height_cm: float
    operator_name: str
    notes: str | None
    created_at: datetime
