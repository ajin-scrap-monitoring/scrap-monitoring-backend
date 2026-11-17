"""Single-valued spatial presentation, separate from observation and decision inputs."""

import math
from statistics import median

from src.app.schemas.monitoring_v2 import LidarProfilePageV2, LidarProfileSampleV2

# A bounded normalized display grid, not an installation or measurement setting.
DISPLAY_BINS = 200


def spatial_profile_display(page: LidarProfilePageV2) -> LidarProfilePageV2:
    """Aggregate current filtered points only after all temporal calculations finish.

    Keep original scan statistics, provenance and received-point coverage. Never feed
    this derived page into a measurement window, fill calculation or collection worker.
    The source view retains angle/index correspondence and unpositioned invalid points.
    """
    result = page.model_copy(deep=True)
    for profile in result.profiles:
        if (
            profile.sampleSource != "FILTERED_POINT_CLOUD"
            or profile.valuePolicy != "SENSOR_ANGLE"
            or not profile.samples
        ):
            continue
        bins: list[list[float]] = [[] for _ in range(DISPLAY_BINS)]
        blocked: set[int] = set()
        unpositioned = 0
        for sample in profile.samples:
            position = sample["positionRatio"]
            if position is None or not math.isfinite(position) or not 0 <= position <= 1:
                # No inferred x, interpolation or neighboring-bin backfill.
                unpositioned += 1
                continue
            index = min(DISPLAY_BINS - 1, int(position * DISPLAY_BINS))
            height = sample["height"]
            if not sample["valid"] or height is None or not math.isfinite(height):
                # An explicitly located invalid sample remains a gap even if
                # another reflection lands in the same display bin.
                blocked.add(index)
                continue
            bins[index].append(height)
        samples: list[LidarProfileSampleV2] = []
        for index, heights in enumerate(bins):
            value = float(median(heights)) if heights and index not in blocked else None
            samples.append(
                {
                    "positionRatio": (index + 0.5) / DISPLAY_BINS,
                    "height": value,
                    "valid": value is not None,
                }
            )
        profile.samples = samples
        profile.valuePolicy = "SPATIAL_BIN_MEDIAN"
        profile.displayBinCount = DISPLAY_BINS
        profile.displayCoverageRatio = sum(sample["valid"] for sample in samples) / DISPLAY_BINS
        profile.displayUnpositionedCount = unpositioned
    return result
