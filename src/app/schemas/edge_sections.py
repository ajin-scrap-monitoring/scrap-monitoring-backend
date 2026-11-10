"""Bounded independent-section wire results; accepting data does not apply edge settings."""

import math
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from src.app.schemas.edge_point_cloud import PointCloudSettings

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Ratio = Annotated[float, Field(ge=0, le=1)]
Coordinate = Annotated[float, Field(ge=-1e9, le=1e9)]
UnitComponent = Annotated[float, Field(ge=-1, le=1)]
Counter = Annotated[int, Field(ge=0, le=9007199254740991)]
Verification = Literal["VERIFIED", "UNVERIFIED", "DEFAULT"]
State = Literal["AVAILABLE", "LIMITED", "UNAVAILABLE"]


class SectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def unit_vector(values: list[float], tolerance: float = 1e-8) -> None:
    if not math.isclose(math.fsum(x * x for x in values), 1, abs_tol=tolerance, rel_tol=0):
        raise ValueError("A unit vector is required")


def weights_valid(weights: list[float]) -> None:
    if any(w <= 0 for w in weights) or not math.isclose(
        math.fsum(weights), 1, abs_tol=1e-9, rel_tol=0
    ):
        raise ValueError("Two positive weights summing to one are required")


class SectionFilters(SectionModel):
    quality_min: Annotated[int, Field(ge=0, le=255)]
    distance_min_mm: Annotated[int, Field(ge=1, le=1073741823)]
    distance_max_mm: Annotated[int, Field(ge=1, le=1073741823)]
    height_margin_mm: Annotated[int, Field(ge=0, le=1000000)]
    spatial_radius_bins: Annotated[int, Field(ge=0, le=5)]
    spatial_max_deviation_mm: Annotated[float, Field(ge=0, le=1e9)]

    @model_validator(mode="after")
    def distance_order(self) -> Self:
        if self.distance_min_mm > self.distance_max_mm:
            raise ValueError("Distance bounds must be ordered")
        return self


class SectionWindow(SectionModel):
    scan_count: Annotated[int, Field(ge=1, le=8)]
    min_scans: Annotated[int, Field(ge=1, le=8)]
    max_age_ms: Annotated[int, Field(ge=1, le=1000)]
    pair_tolerance_ms: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def history_size(self) -> Self:
        if self.min_scans > self.scan_count:
            raise ValueError("Minimum scans exceed the window")
        return self


class SectionSettings(SectionModel):
    sensor_id: Identifier
    origin_xy_mm: list[Coordinate] = Field(min_length=2, max_length=2)
    axis_xy: list[UnitComponent] = Field(min_length=2, max_length=2)
    bin_width_mm: Annotated[int, Field(ge=1, le=1000000)]
    floor_z_mm: list[Coordinate] = Field(min_length=1, max_length=512)
    ceiling_z_mm: list[Coordinate] = Field(min_length=1, max_length=512)
    active_bins: list[bool] = Field(min_length=1, max_length=512)
    filters: SectionFilters
    max_interpolation_gap_bins: Annotated[int, Field(ge=0, le=10)]

    @model_validator(mode="after")
    def geometry(self) -> Self:
        unit_vector(self.axis_xy)
        if not len(self.floor_z_mm) == len(self.ceiling_z_mm) == len(self.active_bins):
            raise ValueError("Section arrays must have the same length")
        if not any(self.active_bins) or any(
            top <= floor for floor, top in zip(self.floor_z_mm, self.ceiling_z_mm, strict=True)
        ):
            raise ValueError("An active section with positive capacity is required")
        return self


class FusionComponentSettings(SectionModel):
    sensor_id: Identifier
    weight: Ratio
    calibration: list[Annotated[list[Ratio], Field(min_length=2, max_length=2)]] = Field(
        min_length=2, max_length=128
    )

    @model_validator(mode="after")
    def monotone_curve(self) -> Self:
        if (
            self.calibration[0][0] != 0
            or self.calibration[-1][0] != 1
            or any(
                b[0] <= a[0] or b[1] < a[1]
                for a, b in zip(self.calibration, self.calibration[1:], strict=False)
            )
        ):
            raise ValueError("Curve inputs must increase from zero to one with monotone outputs")
        return self


class FusionSettings(SectionModel):
    method: Literal["CALIBRATED_WEIGHTED_SECTIONS"]
    version: Identifier
    verification_state: Verification
    components: list[FusionComponentSettings] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def components_valid(self) -> Self:
        if len({c.sensor_id for c in self.components}) != 2:
            raise ValueError("Fusion sensors must be distinct")
        weights_valid([c.weight for c in self.components])
        return self


class IndependentJudgment(SectionModel):
    model_type: Literal["INDEPENDENT_SECTIONS_V1"]
    method_version: Identifier
    verification_state: Verification
    sections: list[SectionSettings] = Field(min_length=2, max_length=2)
    window: SectionWindow
    fusion: FusionSettings | None
    point_cloud: PointCloudSettings | None = None

    @model_serializer(mode="wrap")
    def preserve_absent_point_cloud(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if self.point_cloud is None:
            result.pop("point_cloud", None)
        return result

    @field_validator("point_cloud", mode="before")
    @classmethod
    def point_cloud_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Omit point_cloud to disable; explicit null is not a setting")
        return value

    @model_validator(mode="after")
    def sensor_scope(self) -> Self:
        ids = {s.sensor_id for s in self.sections}
        if len(ids) != 2 or self.fusion and {c.sensor_id for c in self.fusion.components} != ids:
            raise ValueError("Sections and fusion must name the same two sensors")
        return self


class SectionBin(SectionModel):
    index: Annotated[int, Field(ge=0, le=511)]
    floor_z_mm: Coordinate
    ceiling_z_mm: Coordinate
    height_mm: Annotated[float, Field(ge=0, le=2e9)] | None
    source: Literal["OBSERVED", "INTERPOLATED", "UNAVAILABLE", "MASKED"]
    sensor_ids: list[Identifier] = Field(max_length=1)

    @model_validator(mode="after")
    def height_valid(self) -> Self:
        if self.ceiling_z_mm <= self.floor_z_mm:
            raise ValueError("Bin capacity must be positive")
        resolved = self.source in ("OBSERVED", "INTERPOLATED")
        if resolved != (self.height_mm is not None):
            raise ValueError("Unresolved and masked bins require null height")
        if self.height_mm is not None and self.height_mm > self.ceiling_z_mm - self.floor_z_mm:
            raise ValueError("Height exceeds the bin capacity")
        if (self.source == "OBSERVED") != bool(self.sensor_ids):
            raise ValueError("Only observed bins name their source sensor")
        return self


class SectionStats(SectionModel):
    sensor_id: Identifier
    scan_count: Annotated[int, Field(ge=1, le=8)]
    input_points: Counter
    retained_points: Counter
    spatial_rejected_bins: Counter

    @model_validator(mode="after")
    def counts(self) -> Self:
        if self.retained_points > self.input_points:
            raise ValueError("Retained points exceed input points")
        return self


class SensorSectionProfile(SectionModel):
    coordinate_frame_id: Identifier
    origin_xy_mm: list[Coordinate] = Field(min_length=2, max_length=2)
    axis_xy: list[UnitComponent] = Field(min_length=2, max_length=2)
    plane_origin_mm: list[Annotated[float, Field(ge=-1e15, le=1e15)]] = Field(
        min_length=3, max_length=3
    )
    plane_normal: list[UnitComponent] = Field(min_length=3, max_length=3)
    area_basis: Literal["VERTICAL_PROJECTION"]
    bin_width_mm: Annotated[int, Field(ge=1, le=1000000)]
    bins: list[SectionBin] = Field(min_length=1, max_length=512)
    area_mm2: Annotated[float, Field(ge=0, le=1e21)] | None
    capacity_area_mm2: Annotated[float, Field(gt=0, le=1e21)]
    area_ratio: Ratio | None
    coverage_ratio: Ratio
    sensor_stats: list[SectionStats] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def profile_valid(self) -> Self:
        unit_vector(self.axis_xy)
        # Plane normals come from calibration rotations, whose wire tolerance is 1e-6.
        unit_vector(self.plane_normal, tolerance=1e-6)
        if [b.index for b in self.bins] != list(range(len(self.bins))):
            raise ValueError("Bins must keep contiguous source indices")
        active = [b for b in self.bins if b.source != "MASKED"]
        if not active:
            raise ValueError("An active bin is required")
        capacity = self.bin_width_mm * math.fsum(b.ceiling_z_mm - b.floor_z_mm for b in active)
        coverage = sum(b.source == "OBSERVED" for b in active) / len(active)
        if not math.isclose(capacity, self.capacity_area_mm2, rel_tol=1e-9, abs_tol=1e-6) or not (
            math.isclose(coverage, self.coverage_ratio, rel_tol=1e-9, abs_tol=1e-9)
        ):
            raise ValueError("Capacity or coverage does not match the active bins")
        if any(b.height_mm is None for b in active):
            if self.area_mm2 is not None or self.area_ratio is not None:
                raise ValueError("Incomplete sections require null area and ratio")
        else:
            area = self.bin_width_mm * math.fsum(b.height_mm or 0 for b in active)
            if (
                self.area_mm2 is None
                or self.area_ratio is None
                or not (
                    math.isclose(area, self.area_mm2, rel_tol=1e-9, abs_tol=1e-6)
                    and math.isclose(area / capacity, self.area_ratio, rel_tol=1e-9, abs_tol=1e-9)
                )
            ):
                raise ValueError("Area and ratio must describe the supplied heights")
        return self


class SectionResult(SectionModel):
    sensor_id: Identifier
    state: State
    reason_codes: list[Identifier]
    profile: SensorSectionProfile | None

    @model_validator(mode="after")
    def result_valid(self) -> Self:
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Section reasons must be unique")
        if "DEFAULT_SETTINGS" in self.reason_codes and self.state == "AVAILABLE":
            raise ValueError("Default settings cannot be marked AVAILABLE")
        if self.profile:
            if self.profile.sensor_stats[0].sensor_id != self.sensor_id or any(
                sid != self.sensor_id for b in self.profile.bins for sid in b.sensor_ids
            ):
                raise ValueError("A section cannot use another sensor's points")
        if self.state != "UNAVAILABLE" and (
            self.profile is None or self.profile.area_ratio is None
        ):
            raise ValueError("Usable sections require a complete profile")
        return self


class FusionComponent(SectionModel):
    sensor_id: Identifier
    section_ratio: Ratio
    calibrated_ratio: Ratio
    weight: Ratio
    anchor_scan_id: Identifier


class FusionResult(SectionModel):
    method: Literal["CALIBRATED_WEIGHTED_SECTIONS"]
    model_version: Identifier
    ratio: Ratio
    pair_delta_ms: Annotated[float, Field(ge=0, le=100)]
    clock_domain_id: Identifier
    components: list[FusionComponent] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def fusion_valid(self) -> Self:
        if len({c.sensor_id for c in self.components}) != 2:
            raise ValueError("Fusion sensors must be distinct")
        weights_valid([c.weight for c in self.components])
        value = math.fsum(c.weight * c.calibrated_ratio for c in self.components)
        if not math.isclose(value, self.ratio, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Fusion ratio must match the weighted components")
        return self
