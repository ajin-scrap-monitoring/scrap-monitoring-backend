"""Optional filtered world coordinates; raw measurements remain immutable."""

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Id = Annotated[str, Field(min_length=1, max_length=128)]
Count = Annotated[int, Field(ge=0, le=9007199254740991)]
Coordinate = Annotated[float, Field(ge=-9007199254740991, le=9007199254740991)]


class CloudModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PointCloudSettings(CloudModel):
    enabled: bool
    processing_version: Literal["ANGULAR_RANGE_MEDIAN_V1"]
    quality_min: Annotated[int, Field(ge=0, le=255)]
    distance_min_mm: Annotated[int, Field(ge=0, le=1073741823)]
    distance_max_mm: Annotated[int, Field(ge=1, le=1073741823)]
    spatial_radius_points: Annotated[int, Field(ge=0, le=8)]
    spatial_min_neighbors: Annotated[int, Field(ge=1, le=16)]
    spatial_max_deviation_mm: Annotated[float, Field(ge=0, le=1e9)]
    spatial_neighbor_spread_mm: Annotated[float, Field(ge=0, le=1e9)]
    max_adjacent_angle_gap_mdeg: Annotated[int, Field(ge=1, le=180000)]
    angle_match_tolerance_mdeg: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.distance_min_mm > self.distance_max_mm or (
            self.spatial_radius_points
            and self.spatial_min_neighbors > 2 * self.spatial_radius_points
        ):
            raise ValueError("Inconsistent point cloud settings")
        return self


class CloudWindow(CloudModel):
    scan_count: Annotated[int, Field(ge=1, le=8)]
    min_scans: Annotated[int, Field(ge=1, le=8)]
    max_age_ms: Annotated[int, Field(ge=1, le=1000)]
    pair_tolerance_ms: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def history(self) -> Self:
        if self.min_scans > self.scan_count:
            raise ValueError("Minimum scans exceed window")
        return self


class CloudProcessing(PointCloudSettings):
    window: CloudWindow


class CloudReference(CloudModel):
    sensor_id: Id
    scan_id: Id
    config_revision: Id
    calibration_version: Id | None
    coordinate_frame_id: Id


def cloud_time(value: object) -> datetime:
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value
    ):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("UTC RFC3339 timestamp required")
    return value


class CloudAnchor(CloudModel):
    scan_id: Id
    stream_instance_id: Id
    sequence: Count
    acquired_at: Annotated[AwareDatetime, BeforeValidator(cloud_time)]
    timestamp_source: Literal["EDGE_SCAN_RECEIVED"]
    clock_domain_id: Id
    acquired_monotonic_ns: Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
    clock_state: Literal["SYNCED", "DEGRADED", "UNSYNCED"]
    clock_offset_ms: float | None
    source_point_count: Count
    selected_point_count: Count


InvalidReason = Literal[
    "NO_SCAN",
    "SDK_INVALID_RANGE",
    "SOURCE_INVALID",
    "LOW_QUALITY",
    "OUT_OF_RANGE",
    "SPATIAL_OUTLIER",
    "INSUFFICIENT_HISTORY",
    "FILTER_UNVERIFIED",
    "CALIBRATION_MISSING",
    "CALIBRATION_UNVERIFIED",
    "CALIBRATION_MISMATCH",
    "COORDINATE_OUT_OF_RANGE",
    "INPUT_EXPIRED_DURING_FILTER",
]


class FilteredPointCloud(CloudModel):
    sensor_id: Id
    state: Literal["AVAILABLE", "LIMITED", "UNAVAILABLE"]
    reason_codes: list[Id]
    coordinate_frame_id: Id
    calibration_version: Id | None
    verification_state: Literal["VERIFIED", "DEFAULT", "UNVERIFIED"]
    processing_version: Literal["ANGULAR_RANGE_MEDIAN_V1"]
    anchor: CloudAnchor | None
    source_scans: list[CloudReference] = Field(max_length=8)
    point_count: Count
    valid_point_count: Count
    point_indices: list[Count]
    angles_mdeg: list[Annotated[int, Field(ge=0, le=359999)]]
    xyz_mm: list[Annotated[list[Coordinate], Field(min_length=3, max_length=3)] | None]
    valid: list[bool]
    invalid_reason_codes: list[Annotated[list[InvalidReason], Field(max_length=1)]]
    sample_counts: list[Annotated[int, Field(ge=0, le=8)]]
    processing: CloudProcessing

    @model_validator(mode="after")
    def correspondence(self) -> Self:
        arrays = (
            self.point_indices,
            self.angles_mdeg,
            self.xyz_mm,
            self.valid,
            self.invalid_reason_codes,
            self.sample_counts,
        )
        if any(len(v) != self.point_count for v in arrays):
            raise ValueError("Point cloud arrays must correspond")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Duplicate cloud reasons")
        if (
            not self.processing.enabled
            or self.processing_version != self.processing.processing_version
        ):
            raise ValueError("Inconsistent cloud processing")
        if len(self.source_scans) > self.processing.window.scan_count or len(
            {s.scan_id for s in self.source_scans}
        ) != len(self.source_scans):
            raise ValueError("Invalid source scan history")
        if self.anchor is None:
            if self.point_count or self.source_scans:
                raise ValueError("Missing anchor requires empty data")
        elif (
            self.anchor.selected_point_count != self.point_count
            or self.anchor.source_point_count < self.point_count
            or not any(s.scan_id == self.anchor.scan_id for s in self.source_scans)
            or any(i >= self.anchor.source_point_count for i in self.point_indices)
        ):
            raise ValueError("Point cloud anchor mismatch")
        if self.point_indices != sorted(set(self.point_indices)):
            raise ValueError("Point indices must be unique and increasing")
        count = 0
        for xyz, valid, reasons, samples in zip(
            self.xyz_mm, self.valid, self.invalid_reason_codes, self.sample_counts, strict=True
        ):
            if samples > len(self.source_scans):
                raise ValueError("Sample count exceeds source history")
            if valid:
                if xyz is None or reasons or samples < self.processing.window.min_scans:
                    raise ValueError("Valid coordinates require sufficient samples and no reason")
                count += 1
            elif xyz is not None or not reasons:
                raise ValueError("Invalid coordinates require null and a reason")
        expected = (
            "UNAVAILABLE"
            if not count
            else (
                "AVAILABLE"
                if count == self.point_count and self.verification_state == "VERIFIED"
                else "LIMITED"
            )
        )
        if count != self.valid_point_count or self.state != expected:
            raise ValueError("Cloud state or valid count mismatch")
        if count and (
            self.verification_state == "UNVERIFIED"
            or self.calibration_version is None
            or any(s.calibration_version != self.calibration_version for s in self.source_scans)
        ):
            raise ValueError("Valid coordinates require matching installation metadata")
        return self
