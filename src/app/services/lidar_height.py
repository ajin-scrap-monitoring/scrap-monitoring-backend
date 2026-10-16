"""Bounded display projection of verified reflection coordinates, separate from edge decisions."""

import math
from datetime import datetime, timedelta
from statistics import fmean, median

from src.app.schemas.edge_v2 import Configuration, Measurement, SensorObservation
from src.app.schemas.lidar_height import HeightReference
from src.app.schemas.monitoring_v2 import LidarProfileV2


def height_profile(
    measurement: Measurement,
    sensor: SensorObservation,
    configuration: Configuration | None,
    reference: HeightReference,
    depth_cm: float | None,
    now: datetime,
    ttl_seconds: int,
    reference_distance_cm: float,
) -> LidarProfileV2:
    """Use z-up site coordinates, keep missing bins and reject out-of-region reflections.

    Range is a slanted laser distance, never the sensor-to-floor vertical distance.
    A bin median rejects isolated reflections without overweighting dense scan regions.
    """
    scan = sensor.scan
    result = LidarProfileV2(
        sensorId=sensor.sensor_id,
        measuredAt=scan.acquired_at if scan else None,
        configRevision=measurement.config_revision,
        calibrationVersion=sensor.calibration_version,
        referenceDistanceCm=reference_distance_cm,
    )
    reason: str | None = None
    if not reference.verified:
        reason = "HEIGHT_REFERENCE_UNVERIFIED"
    elif configuration is None:
        reason = "HEIGHT_CONFIGURATION_UNAVAILABLE"
    elif (
        (measurement.site_id, measurement.edge_id) != (configuration.site_id, configuration.edge_id)
        or measurement.config_revision != reference.config_revision
        or configuration.config_revision != reference.config_revision
        or measurement.coordinate_frame_id != reference.coordinate_frame_id
        or configuration.coordinate_frame.frame_id != reference.coordinate_frame_id
    ):
        reason = "HEIGHT_REFERENCE_MISMATCH"
    elif depth_cm is None or not math.isfinite(depth_cm) or not 0 < depth_cm <= 1_000_000:
        reason = "HEIGHT_DEPTH_UNAVAILABLE"
    elif measurement.delivery_kind != "PERIODIC":
        reason = "HEIGHT_LIVE_OBSERVATION_REQUIRED"
    elif measurement.generated_at > now or scan is not None and scan.acquired_at > now:
        reason = "HEIGHT_CLOCK_SKEW"
    elif scan is None:
        reason = "HEIGHT_SCAN_UNAVAILABLE"
    elif now - scan.acquired_at >= timedelta(seconds=ttl_seconds):
        reason = "HEIGHT_SCAN_STALE"
    elif scan.clock_state != "SYNCED":
        reason = "HEIGHT_SCAN_CLOCK_UNAVAILABLE"
    else:
        configured = next(
            (s for s in configuration.sensors if s.sensor_id == sensor.sensor_id), None
        )
        calibration = configured.calibration if configured else None
        if (
            sensor.transform_state != "VERIFIED"
            or sensor.transform_reason_codes
            or calibration is None
            or calibration.verification_state != "VERIFIED"
            or sensor.calibration_version != calibration.version
            or reference.calibration_versions.get(sensor.sensor_id) != calibration.version
            or configured is None
            or sensor.angle_intervals_mdeg != configured.angle_intervals_mdeg
        ):
            reason = "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"
    if reason:
        result.reasonCodes = [reason]
        if reason != "HEIGHT_REFERENCE_UNVERIFIED":
            result.calibrationState = "UNAVAILABLE"
        return result

    assert scan is not None and depth_cm is not None
    result.calibrationState = "VERIFIED"
    bins: list[list[float]] = [[] for _ in range(reference.bin_count)]
    low = reference.x_min_cm if reference.profile_axis == "x" else reference.y_min_cm
    high = reference.x_max_cm if reference.profile_axis == "x" else reference.y_max_cm
    for point in scan.points:
        position = point.position_mm
        if (
            position is None
            or point.distance_mm <= 0
            or point.calculation_reason_codes
            or reference.minimum_quality is not None
            and point.quality < reference.minimum_quality
        ):
            continue
        # Compare integers before float conversion, including hostile/out-of-range wire values.
        if not (
            reference.x_min_cm * 10 <= position.x <= reference.x_max_cm * 10
            and reference.y_min_cm * 10 <= position.y <= reference.y_max_cm * 10
            and reference.floor_z_cm * 10 <= position.z <= (reference.floor_z_cm + depth_cm) * 10
        ):
            continue
        x, y, z = position.x / 10, position.y / 10, position.z / 10
        height = z - reference.floor_z_cm
        coordinate = x if reference.profile_axis == "x" else y
        index = min(reference.bin_count - 1, int((coordinate - low) / (high - low) * len(bins)))
        bins[index].append(height)
    values = []
    for index, points in enumerate(bins):
        value = float(median(points)) if len(points) >= reference.minimum_points_per_bin else None
        result.samples.append(
            {
                "positionRatio": (index + 0.5) / len(bins),
                "height": value,
                "valid": value is not None,
            }
        )
        if value is not None:
            values.append(value)
    result.coverageRatio = len(values) / len(bins)
    result.reasonCodes = []
    if values:
        result.average, result.minimum, result.maximum = fmean(values), min(values), max(values)
    if not values:
        result.reasonCodes.append("HEIGHT_NO_VALID_BINS")
    elif result.coverageRatio < reference.minimum_coverage:
        result.reasonCodes.append("HEIGHT_INSUFFICIENT_COVERAGE")
    else:
        result.sectionFillPercent = fmean(values) / depth_cm * 100
    return result
