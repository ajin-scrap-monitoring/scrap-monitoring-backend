"""Select explicit height contexts; simulator clocks and geometry stay separately labelled."""

from src.app.core.config import get_settings
from src.app.schemas.lidar_height import HeightReference, SimulationHeightReference

HeightContext = HeightReference | SimulationHeightReference
SIMULATION_METHOD = "SIMULATION_HEIGHT_SECTION_V1:"


def height_reference(pit_id: str) -> HeightContext | None:
    settings = get_settings()
    references: list[HeightContext] = [
        *settings.EDGE_V2_HEIGHT_REFERENCES,
        *settings.EDGE_V2_SIMULATION_HEIGHT_REFERENCES,
    ]
    return next((r for r in references if r.pit_id == pit_id), None)


def reference_depth(reference: HeightContext | None, pit_depth: float | None) -> float | None:
    if isinstance(reference, SimulationHeightReference):
        return reference.top_z_cm - reference.floor_z_cm
    return pit_depth


def reference_sensors(reference: HeightContext) -> set[str]:
    return set(
        reference.sensor_poses
        if isinstance(reference, SimulationHeightReference)
        else reference.calibration_versions
    )


def clock_allowed(pit_id: str, clock: str) -> bool:
    return clock == "SYNCED" or (
        isinstance(height_reference(pit_id), SimulationHeightReference) and clock == "UNSYNCED"
    )


def device_clock_allowed(site_id: str, edge_id: str, clock: str) -> bool:
    source = next(
        (
            s
            for s in get_settings().EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (site_id, edge_id)
        ),
        None,
    )
    return clock == "SYNCED" or source is not None and clock_allowed(source.pit_id, clock)
