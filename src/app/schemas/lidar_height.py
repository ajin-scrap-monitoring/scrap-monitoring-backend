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
