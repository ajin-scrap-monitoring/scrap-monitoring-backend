"""Validate section provenance using scan metadata, never raw point arrays or cached heights."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.app.schemas.edge_sections import SectionResult
from src.app.schemas.edge_v2 import (
    Assessment,
    Configuration,
    Counter,
    DeviceStatus,
    Identifier,
    UtcTime,
)
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.section_profile_projection import SectionCurve, section_curve


class SectionAnchor(BaseModel):
    """Subset of immutable scan_source metadata; expiration markers cannot validate."""

    model_config = ConfigDict(extra="ignore")
    sensor_id: Identifier
    scan_id: Identifier
    stream_instance_id: Identifier
    sequence: Counter
    acquired_at: UtcTime
    clock_state: Identifier
    clock_domain_id: Identifier
    acquired_monotonic_ns: Annotated[str, Field(strict=True, pattern=r"^[0-9]{1,20}$")]


@dataclass(frozen=True)
class ProjectedSection:
    """LIMITED remains LIMITED even when its geometry can be displayed."""

    curve: SectionCurve
    wire_state: Literal["AVAILABLE", "LIMITED", "UNAVAILABLE"] | None
    area_ratio: float | None
    reason_codes: tuple[str, ...]


def project_section(
    sensor_id: str,
    section: SectionResult | None,
    assessment: Assessment | None,
    configuration: Configuration | None,
    reference: SimulationHeightReference | None,
    anchor: SectionAnchor | None,
    status: DeviceStatus,
    now: datetime,
    ttl_seconds: int,
) -> ProjectedSection:
    """A valid curve does not upgrade an Edge judgment or approve operational use."""
    reason = context_error(
        sensor_id, section, assessment, configuration, reference, anchor, status, now, ttl_seconds
    )
    if reason:
        return ProjectedSection(
            SectionCurve([], None, (reason,)), section.state if section else None, None, (reason,)
        )
    assert section is not None and section.profile is not None and reference is not None
    curve = section_curve(sensor_id, section.profile, reference)
    reasons = tuple(
        dict.fromkeys(
            [
                *section.reason_codes,
                *curve.reason_codes,
                "SIMULATION_GEOMETRY",
                "SIMULATION_SOURCE_CLOCK_UNVERIFIED",
            ]
        )
    )
    return ProjectedSection(
        curve,
        section.state,
        section.profile.area_ratio if not curve.reason_codes else None,
        reasons,
    )


def context_error(
    sensor_id: str,
    section: SectionResult | None,
    assessment: Assessment | None,
    configuration: Configuration | None,
    reference: SimulationHeightReference | None,
    anchor: SectionAnchor | None,
    status: DeviceStatus,
    now: datetime,
    ttl_seconds: int,
) -> str | None:
    """Bind the entire profile to the pinned configuration before exposing any bin."""
    if reference is None or reference.coordinate_source != "EDGE_WORLD":
        return "SECTION_REFERENCE_UNSUPPORTED"
    if assessment is None or section is None or section.profile is None:
        return "SECTION_PROFILE_NOT_RECEIVED"
    if configuration is None or configuration.judgment is None:
        return "SECTION_CONFIGURATION_UNAVAILABLE"
    profile = section.profile
    configured = next((s for s in configuration.sensors if s.sensor_id == sensor_id), None)
    settings = next((s for s in configuration.judgment.sections if s.sensor_id == sensor_id), None)
    pose = reference.sensor_poses.get(sensor_id)
    if (
        assessment.assessment_type != "STATE"
        or section.sensor_id != sensor_id
        or assessment.config_revision != reference.config_revision
        or configuration.config_revision != reference.config_revision
        or (assessment.site_id, assessment.edge_id)
        != (configuration.site_id, configuration.edge_id)
        or configuration.coordinate_frame.frame_id != reference.coordinate_frame_id
        or profile.coordinate_frame_id != reference.coordinate_frame_id
        or configured is None
        or settings is None
        or pose is None
    ):
        return "SECTION_REFERENCE_MISMATCH"
    calibration = configured.calibration
    if (
        calibration is None
        or calibration.verification_state != "VERIFIED"
        or reference.calibration_versions.get(sensor_id) != calibration.version
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
            and math.isclose(
                profile.plane_origin_mm[i], calibration.translation_mm[i], abs_tol=1e-6, rel_tol=0
            )
            and math.isclose(
                profile.plane_normal[i], calibration.rotation[i][2], abs_tol=1e-6, rel_tol=0
            )
            for i in range(3)
        )
    ):
        return "SECTION_CALIBRATION_UNVERIFIED_OR_MISMATCHED"
    if (
        profile.origin_xy_mm != settings.origin_xy_mm
        or profile.axis_xy != settings.axis_xy
        or abs(math.fsum(profile.plane_normal[i] * profile.axis_xy[i] for i in range(2))) > 1e-8
        or profile.bin_width_mm != settings.bin_width_mm
        or len(profile.bins) != len(settings.active_bins)
        or any(
            b.floor_z_mm != settings.floor_z_mm[i]
            or b.ceiling_z_mm != settings.ceiling_z_mm[i]
            or (b.source != "MASKED") != settings.active_bins[i]
            for i, b in enumerate(profile.bins)
        )
    ):
        return "SECTION_PROFILE_SETTINGS_MISMATCH"
    if anchor is None or anchor.sensor_id != sensor_id:
        return "SECTION_ANCHOR_UNAVAILABLE"
    evidence = next(
        (
            s
            for s in assessment.evidence.scans
            if s.scan_id == anchor.scan_id and s.sensor_id == sensor_id
        ),
        None,
    )
    if (
        evidence is None
        or evidence.config_revision != reference.config_revision
        or evidence.coordinate_frame_id != reference.coordinate_frame_id
        or evidence.calibration_version != calibration.version
    ):
        return "SECTION_ANCHOR_REFERENCE_MISMATCH"
    component = (
        next((c for c in assessment.fusion_result.components if c.sensor_id == sensor_id), None)
        if assessment.fusion_result
        else None
    )
    if component and component.anchor_scan_id != anchor.scan_id:
        return "SECTION_FUSION_ANCHOR_MISMATCH"
    if (
        assessment.fusion_result
        and assessment.fusion_result.clock_domain_id != anchor.clock_domain_id
    ):
        return "SECTION_FUSION_CLOCK_DOMAIN_MISMATCH"
    ttl = timedelta(seconds=ttl_seconds)
    if (
        not timedelta(0) <= now - assessment.generated_at < ttl
        or not timedelta(0) <= now - anchor.acquired_at < ttl
        or not timedelta(0)
        <= assessment.generated_at - anchor.acquired_at
        <= timedelta(milliseconds=configuration.judgment.window.max_age_ms)
    ):
        return "SECTION_PROFILE_STALE_OR_CLOCK_SKEW"
    sensor_status = (
        next((s for s in status.snapshot.sensors if s.sensor_id == sensor_id), None)
        if status.snapshot
        else None
    )
    if (
        status.connection_state != "REPORTING"
        or status.effective_state not in ("HEALTHY", "DEGRADED")
        or status.snapshot is None
        or status.snapshot.config_revision != assessment.config_revision
        or status.snapshot.generated_at > now
        or status.snapshot.clock.state not in ("SYNCED", "UNSYNCED")
        or anchor.clock_state not in ("SYNCED", "UNSYNCED")
        or sensor_status is None
        or sensor_status.data_state != "FRESH"
        or sensor_status.last_scan_at is None
        or not timedelta(0) <= now - sensor_status.last_scan_at < ttl
    ):
        return "SECTION_DEVICE_OR_SENSOR_UNAVAILABLE"
    if section.state == "AVAILABLE" and configuration.judgment.verification_state != "VERIFIED":
        return "SECTION_JUDGMENT_UNVERIFIED"
    return None
