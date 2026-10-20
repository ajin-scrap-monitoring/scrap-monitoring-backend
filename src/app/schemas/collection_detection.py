"""Small durable detector state; never stores raw scans or declares confirmed completion."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DetectionPhase = Literal["BASELINING", "IDLE", "CANDIDATE", "COLLECTING"]


class CollectionTracker(BaseModel):
    pit_id: str
    scope_token: str
    phase: DetectionPhase = "BASELINING"
    observed_at: datetime
    input_valid: bool = True
    baseline_cm: float | None = None
    current_cm: float | None = None
    stable_since: datetime | None = None
    stable_min_cm: float | None = None
    stable_max_cm: float | None = None
    candidate_since: datetime | None = None
    episode_id: str | None = None
    simulated: bool = False


class DetectionSummary(BaseModel):
    detectionEnabled: bool = False
    detectionState: DetectionPhase | Literal["DISABLED", "UNKNOWN"] = "DISABLED"
    detectionDropMeters: float | None = None
    detectionConfirmSeconds: int
    detectionSettleSeconds: int
    lastDetectedCollectionAt: datetime | None = None
    secondsSinceLastDetection: int | None = Field(default=None, ge=0)
    lastSettledCollectionAt: datetime | None = None
    secondsSinceLastSettlement: int | None = Field(default=None, ge=0)
    detectedCollectionsInWindow: int = Field(default=0, ge=0)
    averageDetectedCollectionCycleSeconds: float | None = Field(default=None, ge=0)
    detectionSimulated: bool | None = None
    detectionReasonCodes: list[str] = Field(default_factory=list)
