"""Current fill alarms are distinct from historical notifications and read state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.app.schemas.presentation import DisplayPercent


class ActiveFillAlertV2(BaseModel):
    id: str
    type: Literal["pre_collection_alert", "collection_required"]
    severity: Literal["warning", "error"]
    title: str
    evaluatedAt: datetime
    loadPercent: DisplayPercent
    simulated: bool
    notificationId: str | None = None


class ActiveFillAlertPageV2(BaseModel):
    pitId: str
    computedAt: datetime
    scope: Literal["FILL_OPERATIONS"] = "FILL_OPERATIONS"
    evaluationState: Literal["NORMAL", "WARNING", "CRITICAL", "UNKNOWN", "ERROR"]
    items: list[ActiveFillAlertV2] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)
