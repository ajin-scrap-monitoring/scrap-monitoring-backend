"""Invented section bins; source values and geometry are never inferred from field data."""

import copy

import pytest
from pydantic import ValidationError

from src.app.schemas.edge_sections import SensorSectionProfile
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.section_profile_projection import section_curve
from tests.test_edge_section_results import section
from tests.test_edge_world_height import REFERENCE


def profile(heights=(0, 75), sources=("OBSERVED", "OBSERVED"), reverse=False, outside=False):
    raw = section("lidar-a", 50)["profile"]
    raw.update(
        origin_xy_mm=[950, 2000 if reverse else -1000 if outside else 0],
        axis_xy=[0, -1 if reverse else 1],
        bin_width_mm=1000,
    )
    for item, height, source in zip(raw["bins"], heights, sources, strict=True):
        item.update(
            height_mm=height,
            source=source,
            sensor_ids=["lidar-a"] if source == "OBSERVED" else [],
        )
    active = [item for item in raw["bins"] if item["source"] != "MASKED"]
    raw["capacity_area_mm2"] = len(active) * 100000
    raw["coverage_ratio"] = sum(item["source"] == "OBSERVED" for item in active) / len(active)
    raw["area_mm2"] = (
        1000 * sum(item["height_mm"] for item in active)
        if all(item["height_mm"] is not None for item in active)
        else None
    )
    raw["area_ratio"] = (
        raw["area_mm2"] / raw["capacity_area_mm2"] if raw["area_mm2"] is not None else None
    )
    return SensorSectionProfile.model_validate(raw)


def test_bin_centres_convert_once_without_modifying_received_profile():
    value = profile()
    original = value.model_dump()
    result = section_curve("lidar-a", value, SimulationHeightReference.model_validate(REFERENCE))
    assert value.model_dump() == original
    assert result.section_length_cm == 200
    assert result.reason_codes == ()
    assert [item["positionRatio"] for item in result.samples] == [0.25, 0.75]
    assert [item["height"] for item in result.samples] == [0, 7.5]
    assert all(item["valid"] and item["binSource"] == "OBSERVED" for item in result.samples)


def test_reversed_section_axis_keeps_original_bin_identity_after_sort():
    result = section_curve(
        "lidar-a", profile(reverse=True), SimulationHeightReference.model_validate(REFERENCE)
    )
    assert [item["positionRatio"] for item in result.samples] == [0.25, 0.75]
    assert [item["sourceIndex"] for item in result.samples] == [1, 0]
    assert [item["height"] for item in result.samples] == [7.5, 0]


@pytest.mark.parametrize(
    "source,height,valid",
    [
        ("OBSERVED", 0, True),
        ("INTERPOLATED", 50, True),
        ("UNAVAILABLE", None, False),
        ("MASKED", None, False),
    ],
)
def test_source_and_null_gaps_are_preserved(source, height, valid):
    value = profile(heights=(height, 75), sources=(source, "OBSERVED"))
    result = section_curve("lidar-a", value, SimulationHeightReference.model_validate(REFERENCE))
    assert len(result.samples) == 2
    first = result.samples[0]
    assert first["binSource"] == source and first["valid"] is valid
    assert first["height"] == (height / 10 if height is not None else None)


def test_outside_bin_is_excluded_without_moving_or_interpolating_its_neighbour():
    result = section_curve(
        "lidar-a", profile(outside=True), SimulationHeightReference.model_validate(REFERENCE)
    )
    assert result.samples == [
        {
            "positionRatio": 0.25,
            "height": 7.5,
            "valid": True,
            "sourceIndex": 1,
            "binSource": "OBSERVED",
        }
    ]


def test_perpendicular_axis_does_not_emit_two_heights_at_the_same_position():
    value = profile().model_copy(update={"axis_xy": [1.0, 0.0]})
    result = section_curve("lidar-a", value, SimulationHeightReference.model_validate(REFERENCE))
    assert result.samples == []
    assert result.reason_codes == ("SECTION_PROFILE_AXIS_DEGENERATE",)


def test_missing_pose_never_infers_a_floor_line():
    result = section_curve(
        "foreign-sensor", profile(), SimulationHeightReference.model_validate(REFERENCE)
    )
    assert result.samples == []
    assert result.reason_codes == ("SECTION_FLOOR_LINE_UNAVAILABLE",)


def test_conflict_is_not_part_of_independent_sensor_section_wire_contract():
    value = profile().model_dump()
    value["bins"][0].update(source="CONFLICT", height_mm=None, sensor_ids=[])
    value["area_mm2"] = value["area_ratio"] = None
    with pytest.raises(ValidationError):
        SensorSectionProfile.model_validate(copy.deepcopy(value))
