"""Read-only estimate of the configured collection threshold, never a control decision."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.app.schemas.presentation import DisplayPercent


class FillForecastV2(BaseModel):
    pitId: str
    computedAt: datetime
    validUntil: datetime
    state: Literal["AVAILABLE", "REACHED", "UNAVAILABLE"] = "UNAVAILABLE"
    method: Literal["RECENT_LINEAR_TREND_V1"] = "RECENT_LINEAR_TREND_V1"
    simulated: bool = False
    inputSource: Literal["EDGE_STATE", "HEIGHT_SECTIONS"]
    currentLoadPercent: DisplayPercent | None = None
    targetThresholdPercent: float
    expectedThresholdAt: datetime | None = None
    remainingSeconds: int | None = Field(default=None, ge=0)
    increasePercentPerMinute: float | None = None
    trendRSquared: float | None = Field(default=None, ge=0, le=1)
    windowFrom: datetime
    windowTo: datetime
    sampleCount: int = Field(default=0, ge=0)
    reasonCodes: list[str] = Field(default_factory=list)
