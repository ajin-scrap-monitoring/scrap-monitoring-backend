"""Development-only raw-range projection; never creates verified sensor coordinates."""

import math
from datetime import datetime, timedelta
from statistics import fmean, median

from src.app.schemas.edge_v2 import Configuration, Measurement, SensorObservation
from src.app.schemas.lidar_height import (
    MAX_HEIGHT_SAMPLES,
    SimulationHeightReference,
    SimulationSensorPose,
)
from src.app.schemas.monitoring_v2 import LidarProfileV2

XY = tuple[float, float]


def inside(point: XY, polygon: list[XY]) -> bool:
    yes = False
    x, y = point
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            yes = not yes
    return yes


def edge_distance(point: XY, a: XY, b: XY) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def floor_section(
    reference: SimulationHeightReference, pose: SimulationSensorPose
) -> tuple[XY, XY, float] | None:
    """Use the longest continuous scan-plane/floor intersection inside the polygon."""
    u, v = pose.direction0, pose.direction90
    normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    size = math.hypot(normal[0], normal[1])
    if size < 1e-9:
        return None
    tangent = (-normal[1] / size, normal[0] / size)
    if tangent[0 if abs(tangent[0]) > abs(tangent[1]) else 1] < 0:
        tangent = (-tangent[0], -tangent[1])

    def signed(p: XY) -> float:
        return (
            normal[0] * (p[0] - pose.origin_cm[0])
            + normal[1] * (p[1] - pose.origin_cm[1])
            + normal[2] * (reference.floor_z_cm - pose.origin_cm[2])
        )

    points: list[XY] = []

    def add(p: XY) -> None:
        if not any(math.dist(p, q) < 1e-6 for q in points):
            points.append(p)

    polygon = reference.boundary_xy_cm
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        sa, sb = signed(a), signed(b)
        if abs(sa) < 1e-9:
            add(a)
        if sa * sb < 0:
            t = sa / (sa - sb)
            add((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    points.sort(key=lambda p: p[0] * tangent[0] + p[1] * tangent[1])
    spans = [
        (a, b)
        for a, b in zip(points, points[1:], strict=False)
        if inside(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), polygon)
    ]
    if not spans:
        return None
    a, b = max(spans, key=lambda span: math.dist(*span))
    length = math.dist(a, b)
    return (a, tangent, length) if length > 1e-6 else None


def simulation_profile(
    measurement: Measurement,
    sensor: SensorObservation,
    configuration: Configuration | None,
    reference: SimulationHeightReference,
    depth_cm: float | None,
    now: datetime,
    ttl_seconds: int,
    reference_distance_cm: float,
) -> LidarProfileV2:
    """Ignore unavailable coordinate transforms, not freshness/configuration/raw-range validity.

    Source UTC is preserved even when unverified. Receipt time never makes a stale scan fresh.
    Geometry comes only from separately pinned simulator configuration, not wire coordinates.
    """
    scan = sensor.scan
    by_angle = reference.sampling_policy == "SENSOR_ANGLE"
    result = LidarProfileV2(
        sensorId=sensor.sensor_id,
        measuredAt=scan.acquired_at if scan else None,
        configRevision=measurement.config_revision,
        calibrationVersion=sensor.calibration_version,
        referenceDistanceCm=reference_distance_cm,
        referenceDepthCm=reference.top_z_cm - reference.floor_z_cm,
        simulated=True,
        timeBasis="SIMULATION_SOURCE_UTC_UNVERIFIED",
        coverageBasis="RECEIVED_POINTS" if by_angle else "SPATIAL_BINS",
        valuePolicy="SENSOR_ANGLE" if by_angle else "SPATIAL_BIN_MEDIAN",
    )
    configured = (
        next((s for s in configuration.sensors if s.sensor_id == sensor.sensor_id), None)
        if configuration
        else None
    )
    pose = reference.sensor_poses.get(sensor.sensor_id)
    reason = None
    if configuration is None:
        reason = "HEIGHT_CONFIGURATION_UNAVAILABLE"
    elif (
        (measurement.site_id, measurement.edge_id) != (configuration.site_id, configuration.edge_id)
        or measurement.config_revision != reference.config_revision
        or configuration.config_revision != reference.config_revision
        or measurement.coordinate_frame_id != reference.coordinate_frame_id
        or configuration.coordinate_frame.frame_id != reference.coordinate_frame_id
        or configured is None
        or sensor.angle_intervals_mdeg != configured.angle_intervals_mdeg
        or pose is None
    ):
        reason = "HEIGHT_REFERENCE_MISMATCH"
    elif measurement.delivery_kind != "PERIODIC":
        reason = "HEIGHT_LIVE_OBSERVATION_REQUIRED"
    elif measurement.generated_at > now or scan is not None and scan.acquired_at > now:
        reason = "HEIGHT_CLOCK_SKEW"
    elif scan is None:
        reason = "HEIGHT_SCAN_UNAVAILABLE"
    elif now - scan.acquired_at >= timedelta(seconds=ttl_seconds):
        reason = "HEIGHT_SCAN_STALE"
    elif scan.clock_state not in ("SYNCED", "UNSYNCED"):
        reason = "HEIGHT_SCAN_CLOCK_UNAVAILABLE"
    elif by_angle and len(scan.points) > MAX_HEIGHT_SAMPLES:
        reason = "HEIGHT_SAMPLE_LIMIT_EXCEEDED"
    line = floor_section(reference, pose) if pose else None
    if reason is None and line is None:
        reason = "SIMULATION_FLOOR_SECTION_UNAVAILABLE"
    result.reasonCodes = ["SIMULATION_GEOMETRY", "SIMULATION_SOURCE_CLOCK_UNVERIFIED"]
    if reason:
        result.calibrationState = "UNAVAILABLE"
        result.reasonCodes.append(reason)
        return result
    assert scan is not None and pose is not None and line is not None
    origin, tangent, length = line
    result.calibrationState = "SIMULATION"
    result.sectionLengthCm = length
    bins: list[list[float]] = [[] for _ in range(reference.bin_count)] if not by_angle else []
    values: list[float] = []
    depth = reference.top_z_cm - reference.floor_z_cm
    polygon = reference.boundary_xy_cm
    for point in (
        sorted(scan.points, key=lambda p: (p.angle_mdeg, p.index)) if by_angle else scan.points
    ):
        sample: dict[str, float | int | bool | None] = {
            "angleMdeg": point.angle_mdeg,
            "sourceIndex": point.index,
            "positionRatio": None,
            "height": None,
            "valid": False,
        }
        if by_angle:
            # Keep invalid received points as gaps; do not invent unreceived angle slots.
            result.samples.append(sample)
        # Bound hostile wire integers before conversion. Coordinate calculation reasons refer
        # to position_mm, which this raw-range path deliberately does not consume.
        if (
            not 0 < point.distance_mm <= 30_000_000
            or reference.minimum_quality is not None
            and point.quality < reference.minimum_quality
        ):
            continue
        angle = math.radians(point.angle_mdeg / 1000)
        direction = tuple(
            u * math.cos(angle) + v * math.sin(angle)
            for u, v in zip(pose.direction0, pose.direction90, strict=True)
        )
        if direction[2] >= -1e-9:
            continue
        x, y, z = (
            p + point.distance_mm / 10 * d for p, d in zip(pose.origin_cm, direction, strict=True)
        )
        height = z - reference.floor_z_cm
        if height < -1e-6 or height > depth + 1e-6 or not inside((x, y), polygon):
            continue
        if any(
            edge_distance((x, y), a, b) < reference.wall_margin_cm
            for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True)
        ):
            continue
        ratio = ((x - origin[0]) * tangent[0] + (y - origin[1]) * tangent[1]) / length
        if not 0 <= ratio <= 1:
            continue
        value = float(max(0, min(depth, height)))
        if by_angle:
            sample.update(positionRatio=ratio, height=value, valid=True)
            values.append(value)
        else:
            bins[min(len(bins) - 1, int(ratio * len(bins)))].append(value)
    if not by_angle:
        for index, points in enumerate(bins):
            bin_value = (
                float(median(points)) if len(points) >= reference.minimum_points_per_bin else None
            )
            result.samples.append(
                {
                    "positionRatio": (index + 0.5) / len(bins),
                    "height": bin_value,
                    "valid": bin_value is not None,
                }
            )
            if bin_value is not None:
                values.append(bin_value)
    result.coverageRatio = len(values) / len(result.samples) if result.samples else 0
    if values:
        result.average, result.minimum, result.maximum = fmean(values), min(values), max(values)
    if not values:
        result.reasonCodes.append("HEIGHT_NO_VALID_POINTS" if by_angle else "HEIGHT_NO_VALID_BINS")
    elif not by_angle and result.coverageRatio < reference.minimum_coverage:
        result.reasonCodes.append("HEIGHT_INSUFFICIENT_COVERAGE")
    else:
        result.sectionFillPercent = fmean(values) / depth * 100
    return result
