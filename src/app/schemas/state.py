"""Real-time scrap state schemas."""

from datetime import datetime

from pydantic import BaseModel, Field


class RealtimeStatePayload(BaseModel):
    """Real-time scrap status pushed to dashboard web clients."""

    pit_id: str
    fill_ratio_percent: float = Field(description="Current scrap fill percentage")
    calculated_height_cm: float = Field(description="Current scrap height in cm")
    state: str = Field(description="NORMAL, WARNING, or CRITICAL")
    measured_at: datetime = Field(description="Timestamp of the latest measurement")
    warning_threshold_percent: float
    critical_threshold_percent: float
    sensor1_status: str
    sensor2_status: str
    is_valid: bool
