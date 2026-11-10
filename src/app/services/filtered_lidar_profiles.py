"""Opt-in graph projection of edge-filtered points, isolated from fill decisions."""

import math
from datetime import datetime, timedelta
from statistics import fmean

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message
from src.app.schemas.edge_point_cloud import FilteredPointCloud
from src.app.schemas.edge_v2 import Assessment, Configuration, DeviceStatus
from src.app.schemas.lidar_height import MAX_HEIGHT_SAMPLES, SimulationHeightReference
from src.app.schemas.monitoring_v2 import (
    LidarProfilePageV2,
    LidarProfileSampleV2,
    LidarProfileV2,
    MonitoringSource,
)
from src.app.services.edge_v2_projection import latest_message
from src.app.services.edge_v2_service import device_status, utc
from src.app.services.height_median_service import project_height_window, read_height_window
from src.app.services.height_reference import clock_allowed, height_reference
from src.app.services.simulation_height import edge_distance, floor_section, inside

WINDOW = "filtered_profile_window"


def cloud_scope(assessment: Assessment | None) -> object:
    """Never combine different filter settings, streams, clocks or installation versions."""
    return (
        [
            [
                c.sensor_id,
                c.coordinate_frame_id,
                c.calibration_version,
                c.verification_state,
                c.processing.model_dump(mode="json"),
                [c.anchor.stream_instance_id, c.anchor.clock_domain_id] if c.anchor else None,
            ]
            for c in sorted(assessment.filtered_point_clouds or [], key=lambda c: c.sensor_id)
        ]
        if assessment
        else []
    )


def project_cloud(
    sensor_id: str,
    assessment: Assessment | None,
    configuration: Configuration | None,
    reference: SimulationHeightReference | None,
    cloud: FilteredPointCloud | None,
    status: DeviceStatus,
    now: datetime,
    pit_id: str,
) -> LidarProfileV2:
    settings = get_settings()
    anchor = cloud.anchor if cloud else None
    result = LidarProfileV2(
        sensorId=sensor_id,
        measuredAt=anchor.acquired_at if anchor else None,
        configRevision=assessment.config_revision if assessment else None,
        calibrationVersion=cloud.calibration_version if cloud else None,
        sampleSource="FILTERED_POINT_CLOUD",
        processingVersion=cloud.processing_version if cloud else None,
        sourceScanId=anchor.scan_id if anchor else None,
        referenceDistanceCm=settings.EDGE_V2_EMPTY_FLOOR_DISTANCE_CM,
        referenceDepthCm=reference.top_z_cm - reference.floor_z_cm if reference else None,
        calibrationState="UNAVAILABLE",
        simulated=reference is not None,
        timeBasis="SIMULATION_SOURCE_UTC_UNVERIFIED" if reference else "SOURCE_UTC",
        coverageBasis="RECEIVED_POINTS",
        valuePolicy="SENSOR_ANGLE",
        reasonCodes=[],
    )
    configured = (
        next((s for s in configuration.sensors if s.sensor_id == sensor_id), None)
        if configuration
        else None
    )
    pose = reference.sensor_poses.get(sensor_id) if reference else None
    sensor_status = (
        next((s for s in status.snapshot.sensors if s.sensor_id == sensor_id), None)
        if status.snapshot
        else None
    )
    ttl = timedelta(seconds=settings.EDGE_V2_ASSESSMENT_TTL_SECONDS)
    reason = None
    if (
        reference is None
        or reference.coordinate_source != "EDGE_WORLD"
        or reference.sampling_policy != "SENSOR_ANGLE"
    ):
        reason = "FILTERED_PROFILE_REFERENCE_UNSUPPORTED"
    elif assessment is None or cloud is None:
        reason = "FILTERED_PROFILE_NOT_RECEIVED"
    elif configuration is None:
        reason = "HEIGHT_CONFIGURATION_UNAVAILABLE"
    elif (
        assessment.config_revision != reference.config_revision
        or configuration.config_revision != reference.config_revision
        or (assessment.site_id, assessment.edge_id)
        != (configuration.site_id, configuration.edge_id)
        or cloud.coordinate_frame_id != reference.coordinate_frame_id
        or configuration.coordinate_frame.frame_id != reference.coordinate_frame_id
        or configured is None
        or pose is None
    ):
        reason = "HEIGHT_REFERENCE_MISMATCH"
    elif anchor is None or cloud.state == "UNAVAILABLE":
        reason = "FILTERED_PROFILE_UNAVAILABLE"
    elif (
        not timedelta(0) <= now - assessment.generated_at < ttl
        or not timedelta(0) <= now - anchor.acquired_at < ttl
    ):
        reason = "FILTERED_PROFILE_STALE_OR_CLOCK_SKEW"
    elif (
        status.connection_state != "REPORTING"
        or status.effective_state not in ("HEALTHY", "DEGRADED")
        or status.snapshot is None
        or status.snapshot.config_revision != assessment.config_revision
        or not clock_allowed(pit_id, status.snapshot.clock.state)
        or not clock_allowed(pit_id, anchor.clock_state)
        or status.snapshot.generated_at > now
        or sensor_status is None
        or sensor_status.data_state != "FRESH"
        or sensor_status.last_scan_at is None
        or not timedelta(0) <= now - sensor_status.last_scan_at < ttl
    ):
        reason = "HEIGHT_DEVICE_OR_SENSOR_UNAVAILABLE"
    elif len(cloud.xyz_mm) > MAX_HEIGHT_SAMPLES:
        reason = "HEIGHT_SAMPLE_LIMIT_EXCEEDED"
    else:
        calibration = configured.calibration if configured else None
        if (
            calibration is None
            or calibration.verification_state != "VERIFIED"
            or cloud.verification_state == "UNVERIFIED"
            or cloud.calibration_version != calibration.version
            or reference.calibration_versions.get(sensor_id) != calibration.version
            or pose is None
            or not all(
                math.isclose(
                    calibration.translation_mm[i] / 10, pose.origin_cm[i], abs_tol=1e-6, rel_tol=0
                )
                and math.isclose(
                    calibration.rotation[i][0], pose.direction0[i], abs_tol=1e-6, rel_tol=0
                )
                and math.isclose(
                    calibration.rotation[i][1], pose.direction90[i], abs_tol=1e-6, rel_tol=0
                )
                for i in range(3)
            )
        ):
            reason = "HEIGHT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"
        elif any(
            not any(
                interval.start_inclusive <= angle < interval.end_exclusive
                for interval in configured.angle_intervals_mdeg
            )
            for angle in cloud.angles_mdeg
        ):
            reason = "FILTERED_PROFILE_ANGLE_OUTSIDE_CONFIGURATION"
        elif (
            configuration.judgment is None
            or configuration.judgment.point_cloud is None
            or cloud.processing.model_dump(mode="json", exclude={"window"})
            != configuration.judgment.point_cloud.model_dump(mode="json")
            or cloud.processing.window.model_dump() != configuration.judgment.window.model_dump()
        ):
            reason = "FILTERED_PROFILE_SETTINGS_MISMATCH"
    if reason:
        result.reasonCodes = [reason]
        return result
    assert cloud is not None and pose is not None and reference is not None
    line = floor_section(reference, pose)
    if line is None:
        result.reasonCodes = ["SIMULATION_FLOOR_SECTION_UNAVAILABLE"]
        return result
    origin, tangent, length = line
    depth = reference.top_z_cm - reference.floor_z_cm
    result.calibrationState = "SIMULATION"
    result.sectionLengthCm = length
    result.reasonCodes = [
        "SIMULATION_GEOMETRY",
        "SIMULATION_SOURCE_CLOCK_UNVERIFIED",
        "EDGE_FILTERED_POINT_CLOUD",
        *cloud.reason_codes,
    ]
    values = []
    # Preserve each received point, including invalid gaps. No raw-point fallback,
    # interpolation, second installation transform or backend point median is applied.
    for i in sorted(
        range(cloud.point_count), key=lambda i: (cloud.angles_mdeg[i], cloud.point_indices[i])
    ):
        sample: LidarProfileSampleV2 = {
            "sourceIndex": cloud.point_indices[i],
            "angleMdeg": cloud.angles_mdeg[i],
            "positionRatio": None,
            "height": None,
            "valid": False,
        }
        result.samples.append(sample)
        xyz = cloud.xyz_mm[i]
        if not cloud.valid[i] or xyz is None or any(abs(v) > 10_000_000 for v in xyz):
            continue
        x, y, z = (v / 10 for v in xyz)
        height = z - reference.floor_z_cm
        if (
            z >= pose.origin_cm[2]
            or not 0 <= height <= depth
            or not inside((x, y), reference.boundary_xy_cm)
        ):
            continue
        if any(
            edge_distance((x, y), a, b) < reference.wall_margin_cm
            for a, b in zip(
                reference.boundary_xy_cm,
                reference.boundary_xy_cm[1:] + reference.boundary_xy_cm[:1],
                strict=True,
            )
        ):
            continue
        ratio = ((x - origin[0]) * tangent[0] + (y - origin[1]) * tangent[1]) / length
        if not 0 <= ratio <= 1:
            continue
        sample.update({"positionRatio": ratio, "height": height, "valid": True})
        values.append(height)
    result.coverageRatio = len(values) / len(result.samples) if result.samples else 0
    if values:
        result.average, result.minimum, result.maximum = fmean(values), min(values), max(values)
        result.sectionFillPercent = result.average / depth * 100
    else:
        result.reasonCodes.append("HEIGHT_NO_VALID_POINTS")
    return result


async def raw_filtered_profiles(
    db: AsyncSession, source: MonitoringSource, now: datetime
) -> tuple[LidarProfilePageV2, Assessment | None]:
    row = await latest_message(db, source.site_id, source.edge_id, "state")
    assessment = Assessment.model_validate(row.payload) if row else None
    configuration = None
    if assessment:
        registered = await db.scalar(
            select(EdgeV2Message)
            .where(
                EdgeV2Message.site_id == source.site_id,
                EdgeV2Message.edge_id == source.edge_id,
                EdgeV2Message.message_type == "edge_configuration",
                EdgeV2Message.config_revision == assessment.config_revision,
            )
            .limit(1)
        )
        configuration = Configuration.model_validate(registered.payload) if registered else None
    context = height_reference(source.pit_id)
    reference = context if isinstance(context, SimulationHeightReference) else None
    status = await device_status(db, source.site_id, source.edge_id, now)
    device = next(
        d
        for d in get_settings().EDGE_V2_DEVICES
        if (d.site_id, d.edge_id) == (source.site_id, source.edge_id)
    )
    clouds = (
        {c.sensor_id: c for c in (assessment.filtered_point_clouds or [])} if assessment else {}
    )
    page = LidarProfilePageV2(
        pitId=source.pit_id,
        profiles=[
            project_cloud(
                sid,
                assessment,
                configuration,
                reference,
                clouds.get(sid),
                status,
                now,
                source.pit_id,
            )
            for sid in device.sensor_ids
        ],
    )
    valid = [p.sectionFillPercent for p in page.profiles if p.sectionFillPercent is not None]
    anchors = [c.anchor.acquired_at for c in clouds.values() if c.anchor]
    tolerance = (
        configuration.judgment.window.pair_tolerance_ms
        if configuration and configuration.judgment
        else 0
    )
    if len(anchors) != 2 or max(anchors) - min(anchors) > timedelta(milliseconds=tolerance):
        page.meanSectionReasonCodes = ["HEIGHT_PAIR_UNMATCHED"]
    elif len(valid) != 2:
        page.meanSectionReasonCodes = ["HEIGHT_SECTION_UNAVAILABLE"]
    else:
        page.meanSectionFillPercent = fmean(valid)
    return page, assessment


async def filtered_lidar_profiles(
    db: AsyncSession, source: MonitoringSource, now: datetime
) -> LidarProfilePageV2:
    page, assessment = await raw_filtered_profiles(db, source, now)
    return await read_height_window(
        db,
        source,
        page,
        now,
        window_kind=WINDOW,
        observation_kind="state",
        scope_extension=cloud_scope(assessment),
    )


async def project_filtered_profiles(
    db: AsyncSession, assessment: Assessment, receipt: EdgeV2Message
) -> None:
    source = next(
        (
            s
            for s in get_settings().EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (assessment.site_id, assessment.edge_id)
        ),
        None,
    )
    if source is None:
        return
    page, _ = await raw_filtered_profiles(db, source, utc(receipt.received_at))
    await project_height_window(
        db,
        source,
        assessment,
        receipt,
        page,
        window_kind=WINDOW,
        scope_extension=cloud_scope(assessment),
    )
