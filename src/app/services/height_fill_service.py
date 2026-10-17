"""Opt-in height input with separate provenance; never manufactures edge capabilities."""

from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest
from src.app.schemas.edge_v2 import DeviceStatus, Measurement
from src.app.schemas.fill_observation import FillObservation
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.schemas.monitoring_v2 import DashboardDataState, MonitoringSource
from src.app.services.edge_v2_service import digest, utc
from src.app.services.height_reference import (
    SIMULATION_METHOD,
    clock_allowed,
    height_reference,
    reference_depth,
    reference_sensors,
)


def height_method(source: MonitoringSource, depth_cm: float | None) -> str | None:
    reference = height_reference(source.pit_id)
    depth_cm = reference_depth(reference, depth_cm)
    if reference is None or depth_cm is None:
        return None
    if not isinstance(reference, SimulationHeightReference) and not reference.verified:
        return None
    prefix = (
        SIMULATION_METHOD
        if isinstance(reference, SimulationHeightReference)
        else "HEIGHT_SECTION_V2:"
    )
    return prefix + digest(
        [reference.model_dump(mode="json"), depth_cm, get_settings().EDGE_V2_MEDIAN_WINDOW_SECONDS]
    )


async def project_height_fill(
    db: AsyncSession, measurement: Measurement, receipt: EdgeV2Message, row: EdgeV2Filter
) -> None:
    from src.app.services.edge_v2_projection import project_input
    from src.app.services.height_median_service import project_height_window
    from src.app.services.monitoring_v2_service import raw_lidar_profiles

    source = next(
        (
            s
            for s in get_settings().EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (measurement.site_id, measurement.edge_id)
        ),
        None,
    )
    if source is None:
        row.payload = {}
        return
    page = await raw_lidar_profiles(db, source, utc(receipt.received_at))
    stabilized = await project_height_window(db, source, measurement, receipt, page)
    if not get_settings().EDGE_V2_HEIGHT_FILL_ENABLED:
        return
    pit = await db.get(PitConfig, source.pit_id)
    method = height_method(source, pit.total_depth_cm if pit else None)
    observed = max(
        (s.scan.acquired_at for s in measurement.sensors if s.scan is not None), default=None
    )
    value = page.meanSectionFillPercent
    simulated = isinstance(height_reference(source.pit_id), SimulationHeightReference)
    observation = FillObservation(
        simulated=simulated,
        time_basis="SIMULATION_SOURCE_UTC_UNVERIFIED" if simulated else "SOURCE_UTC",
        source="HEIGHT_SECTIONS",
        site_id=measurement.site_id,
        edge_id=measurement.edge_id,
        config_revision=measurement.config_revision,
        message_id=measurement.message_id,
        observation_id="height-" + digest([measurement.message_id, method]),
        generated_at=measurement.generated_at,
        received_at=utc(receipt.received_at),
        observed_to=observed,
        rule_version="SIMULATION_HEIGHT_SECTION_V1" if simulated else "HEIGHT_SECTION_V2",
        method_version=method,
        scope_token=method,
        value_percent=value,
        data_state="AVAILABLE" if value is not None else "UNAVAILABLE",
        reason_codes=(
            ["SIMULATION_GEOMETRY", "SIMULATION_SOURCE_CLOCK_UNVERIFIED", "HEIGHT_SECTION_ESTIMATE"]
            if simulated
            else ["HEIGHT_SECTION_ESTIMATE"]
        )
        if value is not None
        else list(
            dict.fromkeys(
                [
                    *page.meanSectionReasonCodes,
                    *(r for p in page.profiles for r in p.reasonCodes),
                ]
            )
        ),
    )
    current = await db.get(EdgeV2Latest, (measurement.site_id, measurement.edge_id, "height_fill"))
    if current is None:
        current = EdgeV2Latest(
            site_id=measurement.site_id,
            edge_id=measurement.edge_id,
            kind="height_fill",
            message_id=measurement.message_id,
            generated_at=measurement.generated_at,
            payload=observation.model_dump(mode="json"),
        )
        db.add(current)
    else:
        current.message_id = measurement.message_id
        current.generated_at = measurement.generated_at
        current.payload = observation.model_dump(mode="json")
    await project_input(
        db, observation, receipt, row, height_filtered_percent=stabilized.meanSectionFillPercent
    )


async def height_snapshot(
    db: AsyncSession,
    source: MonitoringSource,
    device: DeviceStatus,
    pit: PitConfig | None,
    now: datetime,
) -> tuple[
    FillObservation | None,
    DashboardDataState,
    list[str],
    float | None,
    float | None,
    Literal["WARMING", "READY", "UNAVAILABLE"],
]:
    latest = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, "height_fill"))
    if latest is None:
        return None, "NO_DATA", ["HEIGHT_OBSERVATION_NOT_RECEIVED"], None, None, "UNAVAILABLE"
    observation = FillObservation.model_validate(latest.payload)
    reasons = list(observation.reason_codes)
    state: DashboardDataState = "FRESH"
    observed = observation.observed_to
    threshold = pit.critical_threshold_percent if pit else get_settings().EDGE_V2_CRITICAL_THRESHOLD
    reference = height_reference(source.pit_id)
    sensor_states = {s.sensor_id: s for s in device.snapshot.sensors} if device.snapshot else {}
    sensors_ready = reference is not None and all(
        (sensor := sensor_states.get(sensor_id)) is not None
        and sensor.data_state == "FRESH"
        and sensor.last_scan_at is not None
        and timedelta(0)
        <= now - sensor.last_scan_at
        < timedelta(seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
        for sensor_id in reference_sensors(reference)
    )
    if observation.generated_at > now or observed is not None and observed > now:
        state = "UNKNOWN"
        reasons.append("HEIGHT_CLOCK_SKEW")
    elif now - (observed or observation.generated_at) >= timedelta(
        seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS
    ):
        state = "STALE"
        reasons.append("HEIGHT_SCAN_STALE")
    elif (
        observation.method_version != height_method(source, pit.total_depth_cm if pit else None)
        or observation.source != "HEIGHT_SECTIONS"
        or not sensors_ready
        or device.connection_state != "REPORTING"
        or device.effective_state not in ("HEALTHY", "DEGRADED")
        or device.snapshot is None
        or device.snapshot.config_revision != observation.config_revision
        or not clock_allowed(source.pit_id, device.snapshot.clock.state)
        or device.snapshot.generated_at > now
    ):
        state = "UNAVAILABLE"
        reasons.append("HEIGHT_CONTEXT_UNAVAILABLE_OR_CHANGED")
    elif observation.value_percent is None or observed is None:
        state = "UNAVAILABLE"
    row = await db.get(EdgeV2Filter, (source.site_id, source.edge_id))
    raw = observation.value_percent if state == "FRESH" else None
    if state != "FRESH":
        return observation, state, reasons, None, None, "UNAVAILABLE"
    if (
        row is None
        or row.payload.get("message_id") != observation.message_id
        or row.payload.get("inputSource") != "HEIGHT_SECTIONS"
        or row.payload.get("methodVersion") != observation.method_version
        or row.payload.get("threshold") != threshold
    ):
        return observation, "UNKNOWN", [*reasons, "FILTER_UNAVAILABLE"], raw, None, "UNAVAILABLE"
    if row.payload.get("state") == "READY" and row.payload.get("value") is not None:
        return observation, state, reasons, raw, float(row.payload["value"]), "READY"
    filter_state: Literal["WARMING", "UNAVAILABLE"] = (
        "WARMING" if row.payload.get("state") == "WARMING" else "UNAVAILABLE"
    )
    return observation, "UNKNOWN", [*reasons, "FILTER_" + filter_state], raw, None, filter_state
