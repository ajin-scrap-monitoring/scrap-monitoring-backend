"""Authenticated geometry metadata; missing coordinates are never replaced with zero."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GeometryModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class LayoutPoint2(GeometryModel):
    x: float
    y: float


class LayoutPoint3(LayoutPoint2):
    z: float


class LayoutFrame(GeometryModel):
    id: str
    unit: Literal["cm"] = "cm"
    verticalAxis: Literal["z"] = "z"
    handedness: Literal["RIGHT_HANDED"] = "RIGHT_HANDED"
    originDescription: str
    xAxisDescription: str
    yAxisDescription: str
    zAxisDescription: str


class LayoutRegion(GeometryModel):
    kind: Literal["SIMULATION_BOUNDARY", "HEIGHT_MEASUREMENT_REGION"]
    boundary: list[LayoutPoint2] = Field(min_length=3, max_length=64)
    floorZ: float
    topZ: float | None = None


class LayoutMeasurementLine(GeometryModel):
    kind: Literal["SCAN_PLANE_FLOOR_INTERSECTION"] = "SCAN_PLANE_FLOOR_INTERSECTION"
    a: LayoutPoint3
    b: LayoutPoint3
    lengthCm: float = Field(gt=0)


class LayoutProfileMapping(GeometryModel):
    basis: Literal["FLOOR_SECTION", "FRAME_AXIS"]
    axis: Literal["x", "y"] | None = None
    minimumCm: float
    maximumCm: float


class LayoutSensor(GeometryModel):
    sensorId: str
    position: LayoutPoint3 | None = None
    calibrationVersion: str | None = None
    calibrationState: Literal["SIMULATION", "VERIFIED", "UNAVAILABLE"] = "UNAVAILABLE"
    measurementLine: LayoutMeasurementLine | None = None
    profileMapping: LayoutProfileMapping | None = None
    reasonCodes: list[str] = Field(default_factory=list)


class MeasurementLayoutV2(GeometryModel):
    pitId: str
    computedAt: datetime
    unit: Literal["cm"] = "cm"
    state: Literal["SIMULATION", "PARTIAL", "UNVERIFIED", "UNAVAILABLE"] = "UNAVAILABLE"
    source: Literal["SIMULATION_REFERENCE", "HEIGHT_REFERENCE", "NONE"] = "NONE"
    simulated: bool = False
    configRevision: str | None = None
    configurationGeneratedAt: datetime | None = None
    bindingState: Literal["MATCHED", "MISMATCH", "UNKNOWN"] = "UNKNOWN"
    coordinateFrame: LayoutFrame | None = None
    region: LayoutRegion | None = None
    sensors: list[LayoutSensor] = Field(default_factory=list, max_length=2)
    reasonCodes: list[str] = Field(default_factory=list)
