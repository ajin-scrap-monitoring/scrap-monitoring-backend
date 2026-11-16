"""Configuration-bound fusion checks, including non-linear calibration and real pair clocks."""

import pytest

from src.app.services.section_profile_service import fusion_error
from tests.test_section_profile_context import inputs


def fusion_inputs():
    assessment, config, _, first, _ = inputs()
    second = first.model_copy(
        update={
            "sensor_id": "lidar-b",
            "scan_id": "scan-b-1",
            "stream_instance_id": "second-stream",
            "acquired_monotonic_ns": str(int(first.acquired_monotonic_ns) + 1_000_000),
        }
    )
    return (
        assessment,
        config,
        {"lidar-a": first, "lidar-b": second},
        {"lidar-a": 0.25, "lidar-b": 0.75},
    )


def test_non_linear_calibration_uses_configured_weights_and_curve():
    a, c, anchors, ratios = fusion_inputs()
    c.judgment.fusion.components[0].calibration = [[0, 0], [0.5, 0.25], [1, 1]]
    for index, weight in enumerate([0.25, 0.75]):
        c.judgment.fusion.components[index].weight = weight
        a.fusion_result.components[index].weight = weight
    a.fusion_result.components[0].calibrated_ratio = 0.125
    a.fusion_result.ratio = a.fill_estimate.ratio = 0.59375
    assert fusion_error(a, c, anchors, ratios) is None
    # A self-consistent wire sum still must not override the registered calibration.
    a.fusion_result.components[0].calibrated_ratio = 0.2
    a.fusion_result.ratio = a.fill_estimate.ratio = 0.6125
    assert fusion_error(a, c, anchors, ratios) == "SECTION_FUSION_CONFIGURATION_MISMATCH"


@pytest.mark.parametrize("kind", ["version", "clock", "anchor", "reported_delta"])
def test_fusion_requires_matching_version_anchor_and_pair_clock(kind):
    a, c, anchors, ratios = fusion_inputs()
    if kind == "version":
        a.fusion_result.model_version = "different-model"
    elif kind == "clock":
        a.fusion_result.clock_domain_id = "different-clock"
    elif kind == "anchor":
        a.fusion_result.components[0].anchor_scan_id = "different-scan"
    else:
        a.fusion_result.pair_delta_ms = 10
    assert fusion_error(a, c, anchors, ratios) == "SECTION_FUSION_CONTEXT_MISMATCH"


def test_pair_tolerance_is_not_extended_for_clock_rounding_or_missing_fusion():
    a, c, anchors, ratios = fusion_inputs()
    first, second = anchors.values()
    # Identical wall-clock values cannot make this over-limit monotonic pair usable.
    second.acquired_monotonic_ns = str(int(first.acquired_monotonic_ns) + 81_000_000)
    assert fusion_error(a, c, anchors, ratios) == "SECTION_PAIR_UNMATCHED"
    second.acquired_monotonic_ns = str(int(first.acquired_monotonic_ns) + 80_000_000)
    a.fusion_result.pair_delta_ms = 80
    assert fusion_error(a, c, anchors, ratios) is None
    a.fusion_result = None
    assert fusion_error(a, c, anchors, ratios) == "SECTION_FUSION_NOT_RECEIVED"


def test_different_clock_domains_never_use_a_subtracted_monotonic_value():
    a, c, anchors, ratios = fusion_inputs()
    anchors["lidar-b"].clock_domain_id = "another-domain"
    assert fusion_error(a, c, anchors, ratios) == "SECTION_PAIR_UNMATCHED"
