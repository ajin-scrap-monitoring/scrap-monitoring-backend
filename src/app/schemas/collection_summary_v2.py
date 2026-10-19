"""Dashboard statistics backed by confirmed collection completions only."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from src.app.schemas.collection_detection import DetectionSummary


class CollectionSummaryV2(DetectionSummary):
    pitId: str
    computedAt: datetime
    source: Literal["COLLECTION_LOG"] = "COLLECTION_LOG"
    windowFrom: datetime
    windowTo: datetime
    lastCollectionAt: datetime | None = None
    secondsSinceLastCollection: int | None = Field(default=None, ge=0)
    completedCollectionsInWindow: int = Field(ge=0)
    cycleCount: int = Field(ge=0)
    averageCollectionCycleSeconds: float | None = Field(default=None, ge=0)
    reasonCodes: list[str] = Field(default_factory=list)
    lastCollectionActivityAt: datetime | None = None
    secondsSinceLastCollectionActivity: int | None = Field(default=None, ge=0)
    activitySource: Literal["CONFIRMED_COMPLETION", "HEIGHT_DETECTION"] | None = None
