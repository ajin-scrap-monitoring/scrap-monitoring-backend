"""Project received independent-section bins without filtering or interpolating them again."""

import math
from dataclasses import dataclass
from typing import Literal, TypedDict

from src.app.schemas.edge_sections import SensorSectionProfile
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.simulation_height import floor_section

BinSource = Literal["OBSERVED", "INTERPOLATED", "UNAVAILABLE", "MASKED"]


class SectionSample(TypedDict):
    """Keep original bin identity after sorting in the dashboard's A-to-B direction."""

    positionRatio: float
    height: float | None
    valid: bool
    sourceIndex: int
    binSource: BinSource


@dataclass(frozen=True)
class SectionCurve:
    """A geometrical projection, separate from availability and operational approval."""

    samples: list[SectionSample]
    section_length_cm: float | None
    reason_codes: tuple[str, ...]


def section_curve(
    sensor_id: str,
    profile: SensorSectionProfile,
    reference: SimulationHeightReference,
) -> SectionCurve:
    """Convert mm once and project bin centres onto an explicitly configured floor line.

    This function does not establish freshness, calibration, configuration binding or
    permission to use a value for alerts. Callers must validate those prerequisites.
    Physical installations need their own verified plane definition; this path only
    accepts the separately labelled simulation reference.
    """
    pose = reference.sensor_poses.get(sensor_id)
    line = floor_section(reference, pose) if pose is not None else None
    if line is None:
        return SectionCurve([], None, ("SECTION_FLOOR_LINE_UNAVAILABLE",))
    origin, tangent, length = line
    # A perpendicular axis collapses every bin onto one position. Reject the curve
    # instead of discarding one of several contradictory heights at that position.
    step = math.fsum(a * b for a, b in zip(profile.axis_xy, tangent, strict=True))
    if abs(step) <= 1e-9:
        return SectionCurve([], length, ("SECTION_PROFILE_AXIS_DEGENERATE",))
    samples: list[SectionSample] = []
    for bin_value in profile.bins:
        centre = [
            (start + (bin_value.index + 0.5) * profile.bin_width_mm * axis) / 10
            for start, axis in zip(profile.origin_xy_mm, profile.axis_xy, strict=True)
        ]
        position = (
            math.fsum(
                (value - start) * direction
                for value, start, direction in zip(centre, origin, tangent, strict=True)
            )
            / length
        )
        if not 0 <= position <= 1:
            continue
        valid = bin_value.source in ("OBSERVED", "INTERPOLATED") and bin_value.height_mm is not None
        samples.append(
            {
                "positionRatio": position,
                "height": bin_value.height_mm / 10
                if valid and bin_value.height_mm is not None
                else None,
                "valid": valid,
                "sourceIndex": bin_value.index,
                "binSource": bin_value.source,
            }
        )
    samples.sort(key=lambda sample: sample["positionRatio"])
    if any(
        right["positionRatio"] <= left["positionRatio"]
        for left, right in zip(samples, samples[1:], strict=False)
    ):
        return SectionCurve([], length, ("SECTION_PROFILE_POSITION_COLLISION",))
    return SectionCurve(samples, length, () if samples else ("SECTION_PROFILE_OUTSIDE_REFERENCE",))
