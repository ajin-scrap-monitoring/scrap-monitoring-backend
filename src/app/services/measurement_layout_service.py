"""Expose only explicit reference geometry; never infer a scan plane from axis descriptions."""

import math
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Configuration
from src.app.schemas.lidar_height import HeightReference, SimulationHeightReference
from src.app.schemas.measurement_layout import (
    LayoutFrame,
    LayoutMeasurementLine,
    LayoutPoint2,
    LayoutPoint3,
    LayoutProfileMapping,
    LayoutRegion,
    LayoutSensor,
    MeasurementLayoutV2,
)
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.height_reference import height_reference, reference_sensors
from src.app.services.simulation_height import floor_section


async def measurement_layout(
    db: AsyncSession, source: MonitoringSource, now: datetime | None = None
) -> MeasurementLayoutV2:
    device = next(
        d
        for d in get_settings().EDGE_V2_DEVICES
        if (d.site_id, d.edge_id) == (source.site_id, source.edge_id)
    )
    result = MeasurementLayoutV2(
        pitId=source.pit_id,
        computedAt=now or datetime.now(UTC),
        sensors=[LayoutSensor(sensorId=s) for s in device.sensor_ids],
    )
    reference = height_reference(source.pit_id)
    if reference is None:
        result.reasonCodes = ["LAYOUT_REFERENCE_UNAVAILABLE"]
        return result
    result.configRevision = reference.config_revision
    simulated = isinstance(reference, SimulationHeightReference)
    result.simulated = simulated
    result.source = "SIMULATION_REFERENCE" if simulated else "HEIGHT_REFERENCE"
    # Project only revision metadata. A layout query must not load current scan points.
    live_revisions = (
        (
            await db.execute(
                select(EdgeV2Latest.payload["config_revision"].as_string()).where(
                    EdgeV2Latest.site_id == source.site_id,
                    EdgeV2Latest.edge_id == source.edge_id,
                    EdgeV2Latest.kind.in_(["measurement", "status"]),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(r is not None and r != reference.config_revision for r in live_revisions):
        result.bindingState = "MISMATCH"
        result.reasonCodes = ["LAYOUT_LIVE_CONFIG_MISMATCH"]
        return result
    if live_revisions and all(r == reference.config_revision for r in live_revisions):
        result.bindingState = "MATCHED"
    registration = await db.scalar(
        select(EdgeV2Message)
        .where(
            EdgeV2Message.site_id == source.site_id,
            EdgeV2Message.edge_id == source.edge_id,
            EdgeV2Message.message_type == "edge_configuration",
            EdgeV2Message.config_revision == reference.config_revision,
        )
        .order_by(EdgeV2Message.id.desc())
        .limit(1)
    )
    if registration is None:
        result.reasonCodes = ["LAYOUT_CONFIGURATION_UNAVAILABLE"]
        return result
    config = Configuration.model_validate(registration.payload)
    if (
        config.coordinate_frame.frame_id != reference.coordinate_frame_id
        or set(device.sensor_ids) != reference_sensors(reference)
        or {s.sensor_id for s in config.sensors} != set(device.sensor_ids)
    ):
        result.reasonCodes = ["LAYOUT_REFERENCE_MISMATCH"]
        return result
    frame = config.coordinate_frame
    result.configurationGeneratedAt = config.generated_at
    result.coordinateFrame = LayoutFrame(
        id=frame.frame_id,
        originDescription=frame.origin_description,
        xAxisDescription=frame.x_axis_description,
        yAxisDescription=frame.y_axis_description,
        zAxisDescription=frame.z_axis_description,
    )
    if isinstance(reference, SimulationHeightReference):
        _simulation(result, reference)
    else:
        pit = await db.get(PitConfig, source.pit_id)
        _physical(result, reference, config, pit.total_depth_cm if pit else None)
    return result


def _simulation(result: MeasurementLayoutV2, reference: SimulationHeightReference) -> None:
    result.state = "SIMULATION"
    result.reasonCodes = ["SIMULATION_GEOMETRY", "PHYSICAL_INSTALLATION_UNVERIFIED"]
    result.region = LayoutRegion(
        kind="SIMULATION_BOUNDARY",
        boundary=[LayoutPoint2(x=x, y=y) for x, y in reference.boundary_xy_cm],
        floorZ=reference.floor_z_cm,
        topZ=reference.top_z_cm,
    )
    for sensor in result.sensors:
        pose = reference.sensor_poses[sensor.sensorId]
        sensor.position = LayoutPoint3(
            x=pose.origin_cm[0], y=pose.origin_cm[1], z=pose.origin_cm[2]
        )
        sensor.calibrationState = "SIMULATION"
        line = floor_section(reference, pose)
        if line is None:
            sensor.reasonCodes = ["LAYOUT_FLOOR_SECTION_UNAVAILABLE"]
            result.state = "PARTIAL"
            continue
        a, tangent, length = line
        sensor.measurementLine = LayoutMeasurementLine(
            a=LayoutPoint3(x=a[0], y=a[1], z=reference.floor_z_cm),
            b=LayoutPoint3(
                x=a[0] + tangent[0] * length, y=a[1] + tangent[1] * length, z=reference.floor_z_cm
            ),
            lengthCm=length,
        )
        sensor.profileMapping = LayoutProfileMapping(
            basis="FLOOR_SECTION", minimumCm=0, maximumCm=length
        )


def _physical(
    result: MeasurementLayoutV2,
    reference: HeightReference,
    config: Configuration,
    depth: float | None,
) -> None:
    if not reference.verified:
        result.state = "UNVERIFIED"
        result.reasonCodes = ["HEIGHT_REFERENCE_UNVERIFIED"]
        return
    # This rectangle is the configured height calculation region, not a surveyed pit outline.
    top = (
        reference.floor_z_cm + depth
        if depth is not None and math.isfinite(depth) and 0 < depth <= 1_000_000
        else None
    )
    result.region = LayoutRegion(
        kind="HEIGHT_MEASUREMENT_REGION",
        floorZ=reference.floor_z_cm,
        topZ=top,
        boundary=[
            LayoutPoint2(x=x, y=y)
            for x, y in [
                (reference.x_min_cm, reference.y_min_cm),
                (reference.x_max_cm, reference.y_min_cm),
                (reference.x_max_cm, reference.y_max_cm),
                (reference.x_min_cm, reference.y_max_cm),
            ]
        ],
    )
    result.state = "PARTIAL"
    result.reasonCodes = ["PHYSICAL_PIT_BOUNDARY_NOT_CONFIGURED", "PHYSICAL_SCAN_PLANE_UNSPECIFIED"]
    if top is None:
        result.reasonCodes.append("HEIGHT_DEPTH_UNAVAILABLE")
    for sensor in result.sensors:
        calibration = next(s.calibration for s in config.sensors if s.sensor_id == sensor.sensorId)
        sensor.calibrationVersion = calibration.version if calibration else None
        if (
            calibration is None
            or calibration.verification_state != "VERIFIED"
            or calibration.version != reference.calibration_versions.get(sensor.sensorId)
        ):
            sensor.reasonCodes = ["LAYOUT_CALIBRATION_UNVERIFIED_OR_MISMATCHED"]
            continue
        x, y, z = calibration.translation_mm
        sensor.position = LayoutPoint3(x=x / 10, y=y / 10, z=z / 10)
        sensor.calibrationState = "VERIFIED"
        sensor.reasonCodes = ["PHYSICAL_SCAN_PLANE_UNSPECIFIED"]
        sensor.profileMapping = LayoutProfileMapping(
            basis="FRAME_AXIS",
            axis=reference.profile_axis,
            minimumCm=reference.x_min_cm if reference.profile_axis == "x" else reference.y_min_cm,
            maximumCm=reference.x_max_cm if reference.profile_axis == "x" else reference.y_max_cm,
        )
