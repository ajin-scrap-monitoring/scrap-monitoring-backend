"""Bounded v2 trend samples; missing/invalid reports never become zero percent."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.schemas.presentation import DisplayPercent

HistoryInterval = Literal[60, 300, 900, 3600, 7200, 21600, 86400]
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
    valuePercent: DisplayPercent | None = Field(default=None, ge=0, le=100)
    valid: bool = False
    dataState: HistoryDataState = "NO_DATA"
    reasonCodes: list[str] = Field(default_factory=lambda: ["STATE_NOT_RECEIVED"])
    averagePercent: DisplayPercent | None = None
    minimumPercent: DisplayPercent | None = None
    maximumPercent: DisplayPercent | None = None
    validSamples: int = 0
    invalidSamples: int = 0
    valuePolicy: Literal["RAW_STATE", "MEDIAN_MINUTE"] = "RAW_STATE"


class HistoryWindowV2(BaseModel):
    """Shared query window; the full view retains its existing JSON fields."""

    model_config = ConfigDict(populate_by_name=True)

    schemaVersion: Literal["2"] = "2"
    pitId: str
    source: MonitoringSource
    from_time: datetime = Field(alias="from")
    to_time: datetime = Field(alias="to")
    bucketSeconds: HistoryInterval
    selectionPolicy: Literal["LATEST_STATE_PER_BUCKET"] = "LATEST_STATE_PER_BUCKET"


class LoadHistoryV2(HistoryWindowV2):
    samples: list[HistorySampleV2]


class ChartHistorySampleV2(HistorySampleV2):
    """Keep numerical and quality evidence while omitting long provenance identifiers."""

    assessmentId: str | None = Field(default=None, exclude=True)
    observationId: str | None = Field(default=None, exclude=True)
    methodVersion: str | None = Field(default=None, exclude=True)


class ChartHistoryV2(HistoryWindowV2):
    view: Literal["chart"] = "chart"
    samples: list[ChartHistorySampleV2]

    @classmethod
    def from_history(cls, history: LoadHistoryV2) -> "ChartHistoryV2":
        """Project an already computed history, without another DB read or calculation."""
        return cls.model_validate(
            dict(history, samples=[dict(sample) for sample in history.samples])
        )
