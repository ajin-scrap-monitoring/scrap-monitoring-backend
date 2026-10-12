"""Dashboard v2 contract: unknown values stay null and source judgments remain visible."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.edge_v2 import Assessment, DeviceStatus, Identifier

DashboardDataState = Literal["FRESH", "STALE", "UNAVAILABLE", "UNKNOWN", "NO_DATA"]


class MonitoringSource(BaseModel):
    """Explicit one-to-one pit/device assignment; never infer a pit from wire IDs."""

    model_config = ConfigDict(extra="forbid")

    pit_id: Identifier
    site_id: Identifier
    edge_id: Identifier


class MonitoringSummaryV2(BaseModel):
    loadPercent: float | None = Field(default=None, ge=0, le=100)
    warningThresholdPercent: float
    collectionThresholdPercent: float


class MonitoringSnapshotV2(BaseModel):
    schemaVersion: Literal["2"] = "2"
    snapshotId: str
    serverTime: datetime
    pitId: str
    source: MonitoringSource
    measuredAt: datetime | None
    assessmentReceivedAt: datetime | None
    dataState: DashboardDataState
    reasonCodes: list[str]
    summary: MonitoringSummaryV2
    assessment: Assessment | None
    deviceStatus: DeviceStatus
