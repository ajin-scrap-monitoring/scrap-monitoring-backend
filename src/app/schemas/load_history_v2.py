"""Bounded v2 trend samples; missing/invalid reports never become zero percent."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.monitoring_v2 import MonitoringSource

HistoryInterval = Literal[60, 300, 900, 3600]
HistoryDataState = Literal["AVAILABLE", "LIMITED", "UNAVAILABLE", "STALE", "UNKNOWN", "NO_DATA"]


class HistorySampleV2(BaseModel):
    simulated: bool = False
    bucketStartAt: datetime
    bucketEndAt: datetime
    generatedAt: datetime | None = None
    observedAt: datetime | None = None
    assessmentId: str | None = None
    observationId: str | None = None
    inputSource: Literal["EDGE_STATE", "HEIGHT_SECTIONS", "MIXED"] = "EDGE_STATE"
    methodVersion: str | None = None
    valuePercent: float | None = Field(default=None, ge=0, le=100)
    valid: bool = False
    dataState: HistoryDataState = "NO_DATA"
    reasonCodes: list[str] = Field(default_factory=lambda: ["STATE_NOT_RECEIVED"])
    averagePercent: float | None = None
    minimumPercent: float | None = None
    maximumPercent: float | None = None
    validSamples: int = 0
    invalidSamples: int = 0
    valuePolicy: Literal["RAW_STATE", "MEDIAN_MINUTE"] = "RAW_STATE"


class LoadHistoryV2(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schemaVersion: Literal["2"] = "2"
    pitId: str
    source: MonitoringSource
    from_time: datetime = Field(alias="from")
    to_time: datetime = Field(alias="to")
    bucketSeconds: HistoryInterval
    selectionPolicy: Literal["LATEST_STATE_PER_BUCKET"] = "LATEST_STATE_PER_BUCKET"
    samples: list[HistorySampleV2]
