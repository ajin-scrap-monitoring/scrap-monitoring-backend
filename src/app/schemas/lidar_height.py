"""Private, explicit geometry for a height reference; never infer axes from descriptions."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.app.schemas.edge_v2 import Identifier


class HeightReference(BaseModel):
    """A pinned z-up frame, flat floor and rectangular region, all in centimetres."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)

    pit_id: Identifier
    config_revision: Identifier
    coordinate_frame_id: Identifier
    calibration_versions: dict[Identifier, Identifier] = Field(min_length=2, max_length=2)
    verified: bool = False
    floor_z_cm: float = Field(ge=-1_000_000, le=1_000_000)
    x_min_cm: float = Field(ge=-1_000_000, le=1_000_000)
    x_max_cm: float = Field(ge=-1_000_000, le=1_000_000)
    y_min_cm: float = Field(ge=-1_000_000, le=1_000_000)
    y_max_cm: float = Field(ge=-1_000_000, le=1_000_000)
    profile_axis: Literal["x", "y"]
    bin_count: int = Field(default=20, ge=2, le=200)
    minimum_points_per_bin: int = Field(default=3, ge=1, le=1000)
    minimum_coverage: float = Field(default=1.0, gt=0, le=1)
    # SDK quality has no universal interpretation; a cutoff must be configured explicitly.
    minimum_quality: int | None = None

    @model_validator(mode="after")
    def positive_region(self) -> Self:
        if self.x_min_cm >= self.x_max_cm or self.y_min_cm >= self.y_max_cm:
            raise ValueError("Height reference region must have positive width on both axes")
        return self


class SimulationSensorPose(BaseModel):
    """Explicit simulator pose; vectors are unit directions, positions are cm."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    origin_cm: tuple[float, float, float]
    direction0: tuple[float, float, float]
    direction90: tuple[float, float, float]

    @model_validator(mode="after")
    def orthonormal_directions(self) -> Self:
        import math

        if any(abs(v) > 1_000_000 for v in self.origin_cm):
            raise ValueError("Simulation origin is out of bounds")
        if any(abs(math.hypot(*v) - 1) > 1e-6 for v in (self.direction0, self.direction90)):
            raise ValueError("Simulation directions must be unit vectors")
        if abs(sum(a * b for a, b in zip(self.direction0, self.direction90, strict=True))) > 1e-6:
            raise ValueError("Simulation directions must be orthogonal")
        return self


class SimulationHeightReference(BaseModel):
    """Development-only raw-range geometry, never a verified physical calibration."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    pit_id: Identifier
    config_revision: Identifier
    coordinate_frame_id: Identifier
    environment_id: Identifier
    simulator_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    floor_z_cm: float = Field(ge=-1_000_000, le=1_000_000)
    top_z_cm: float = Field(ge=-1_000_000, le=1_000_000)
    boundary_xy_cm: list[tuple[float, float]] = Field(min_length=3, max_length=64)
    sensor_poses: dict[Identifier, SimulationSensorPose] = Field(min_length=2, max_length=2)
    bin_count: int = Field(default=20, ge=2, le=200)
    minimum_points_per_bin: int = Field(default=3, ge=1, le=1000)
    minimum_coverage: float = Field(default=1.0, gt=0, le=1)
    wall_margin_cm: float = Field(default=5, ge=0, le=1000)
    minimum_quality: int | None = None

    @model_validator(mode="after")
    def valid_geometry(self) -> Self:
        if not 0 < self.top_z_cm - self.floor_z_cm <= 1_000_000:
            raise ValueError("Simulation depth must be positive and bounded")
        polygon = self.boundary_xy_cm
        if any(abs(v) > 1_000_000 for point in polygon for v in point):
            raise ValueError("Simulation boundary is out of bounds")
        edges = list(zip(polygon, polygon[1:] + polygon[:1], strict=True))
        if any(a == b for a, b in edges):
            raise ValueError("Simulation boundary has a zero-length edge")
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)
        if abs(area) < 1e-6:
            raise ValueError("Simulation boundary must have positive area")

        def cross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        for i, (a, b) in enumerate(edges):
            for j, (c, d) in enumerate(edges):
                if j <= i + 1 or i == 0 and j == len(edges) - 1:
                    continue
                # Reject crossing, touching or overlapping non-adjacent edges.
                if (
                    max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))
                    and max(min(a[1], b[1]), min(c[1], d[1]))
                    <= min(max(a[1], b[1]), max(c[1], d[1]))
                    and cross(a, b, c) * cross(a, b, d) <= 0
                    and cross(c, d, a) * cross(c, d, b) <= 0
                ):
                    raise ValueError("Simulation boundary must be a simple polygon")
        return self
