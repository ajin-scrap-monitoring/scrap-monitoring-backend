"""Current fill alarms are distinct from historical notifications and read state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.app.schemas.presentation import DisplayPercent
from src.app.schemas.response_fields import RequiredResponseFields


class ActiveFillAlertV2(BaseModel):
    id: str
    type: Literal["pre_collection_alert", "collection_required"]
    severity: Literal["warning", "error"]
    title: str
    evaluatedAt: datetime
    measuredAt: datetime | None = None
    evaluationSource: Literal["CURRENT_OBSERVATION", "RECENT_PERSISTED"] | None = None
    loadPercent: DisplayPercent
    simulated: bool
    notificationId: str | None = None
    startedAt: datetime | None = None
    startBasis: Literal["SERVER_EVALUATION", "UNKNOWN"] = "UNKNOWN"


class ActiveHealthAlertV2(BaseModel):
    id: str
    type: Literal["device_error", "measurement_error"]
    severity: Literal["warning", "error"]
    title: str
    componentKind: Literal["EDGE", "LIDAR", "MEASUREMENT"]
    componentId: str
    state: str
    evaluatedAt: datetime
    measuredAt: datetime | None = None
    startedAt: datetime | None = None
    startBasis: Literal["SERVER_EVALUATION", "UNKNOWN"] = "UNKNOWN"
    evidenceSource: Literal["EDGE_STATUS", "STATUS_FRESHNESS", "CURRENT_MEASUREMENT"]
    reasonCodes: list[str] = Field(default_factory=list)
    simulated: bool | None = None


class ActiveHealthCoverageV2(BaseModel):
    componentKind: Literal["EDGE", "LIDAR", "MEASUREMENT", "CAMERA"]
    componentId: str | None = None
    state: Literal["CHECKED", "UNKNOWN", "UNSUPPORTED"]
    reasonCodes: list[str] = Field(default_factory=list)


class ActiveFillAlertPageV2(RequiredResponseFields):
    # Defaults simplify internal construction but coverage is always emitted.
    required_response_fields = ("coverage",)

    pitId: str
    computedAt: datetime
    scope: Literal["FILL_OPERATIONS", "ALL"] = "FILL_OPERATIONS"
    evaluationState: Literal["NORMAL", "WARNING", "CRITICAL", "UNKNOWN", "ERROR"]
    items: list[ActiveFillAlertV2 | ActiveHealthAlertV2] = Field(default_factory=list)
    reasonCodes: list[str] = Field(default_factory=list)

    coverage: list[ActiveHealthCoverageV2] = Field(default_factory=list)
