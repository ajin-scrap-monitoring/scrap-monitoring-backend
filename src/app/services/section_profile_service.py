"""One explicit section/fusion input for curves, live values, and persisted scalar history."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Filter
from src.app.schemas.edge_v2 import Assessment, Configuration, DeviceStatus
from src.app.schemas.fill_observation import FillObservation
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.schemas.monitoring_v2 import (
    DashboardDataState,
    LidarProfilePageV2,
    LidarProfileSampleV2,
    LidarProfileV2,
    MonitoringSource,
)
from src.app.services.edge_v2_projection import latest_message
from src.app.services.edge_v2_service import device_status, digest, utc
from src.app.services.height_reference import height_reference, reference_payload
from src.app.services.section_profile_context import SectionAnchor, project_section


@dataclass(frozen=True)
class SectionFrame:
    page: LidarProfilePageV2
    observation: FillObservation | None
    retain_pair_history: bool


async def scan_anchors(
    db: AsyncSession, assessment: Assessment, sensor_ids: tuple[str, str]
) -> dict[str, SectionAnchor]:
    """Two indexed scan_source lookups; never fetch scan_result point arrays.

    A matching cloud anchor also carries scan timing when periodic raw delivery is
    not configured. If both exist, their immutable metadata must agree.
    """
    clouds = {c.sensor_id: c for c in assessment.filtered_point_clouds or []}
    identities = (
        {c.sensor_id: c.anchor_scan_id for c in assessment.fusion_result.components}
        if assessment.fusion_result
        else {
            cloud.sensor_id: cloud.anchor.scan_id
            for cloud in clouds.values()
            if cloud.sensor_id in sensor_ids and cloud.anchor is not None
        }
    )
    if assessment.fusion_result is None:
        for sid in sensor_ids:
            evidence = [r for r in assessment.evidence.scans if r.sensor_id == sid]
            if sid not in identities and len(evidence) == 1:
                identities[sid] = evidence[0].scan_id
    result: dict[str, SectionAnchor] = {}
    for sid in sensor_ids:
        scan_id = identities.get(sid)
        if scan_id is None:
            continue
        row = (
            await db.execute(
                select(EdgeV2Artifact.artifact_key, EdgeV2Artifact.payload).where(
                    EdgeV2Artifact.site_id == assessment.site_id,
                    EdgeV2Artifact.edge_id == assessment.edge_id,
                    EdgeV2Artifact.kind == "scan_source",
                    EdgeV2Artifact.key_hash == digest([scan_id]),
                )
            )
        ).one_or_none()
        cloud = clouds.get(sid)
        cloud_anchor = (
            cloud.anchor if cloud and cloud.anchor and cloud.anchor.scan_id == scan_id else None
        )
        try:
            stored = SectionAnchor.model_validate(row.payload) if row else None
            supplied = (
                SectionAnchor.model_validate({**cloud_anchor.model_dump(), "sensor_id": sid})
                if cloud_anchor
                else None
            )
        except ValidationError:
            continue
        if row and (row.artifact_key != [scan_id] or stored is None or stored.sensor_id != sid):
            continue
        if stored is not None and supplied is not None and stored != supplied:
            continue
        anchor = stored or supplied
        if anchor is not None and anchor.scan_id == scan_id:
            result[sid] = anchor
    return result


def fusion_error(
    assessment: Assessment,
    configuration: Configuration | None,
    anchors: dict[str, SectionAnchor],
    section_ratios: dict[str, float],
) -> str | None:
    """Use the common monotonic clock; never extend the configured pair tolerance."""
    judgment = configuration.judgment if configuration else None
    if judgment is None or judgment.fusion is None:
        return "SECTION_FUSION_CONFIGURATION_UNAVAILABLE"
    if len(anchors) != 2:
        return "SECTION_ANCHOR_UNAVAILABLE"
    first, second = anchors.values()
    delta = abs(int(first.acquired_monotonic_ns) - int(second.acquired_monotonic_ns)) / 1_000_000
    if first.clock_domain_id != second.clock_domain_id or delta > judgment.window.pair_tolerance_ms:
        return "SECTION_PAIR_UNMATCHED"
    fusion = assessment.fusion_result
    if fusion is None:
        return "SECTION_FUSION_NOT_RECEIVED"
    if (
        fusion.clock_domain_id != first.clock_domain_id
        or not math.isclose(fusion.pair_delta_ms, delta, abs_tol=0.001, rel_tol=0)
        or fusion.model_version != judgment.fusion.version
        or fusion.method != judgment.fusion.method
        or set(section_ratios) != {c.sensor_id for c in fusion.components}
    ):
        return "SECTION_FUSION_CONTEXT_MISMATCH"
    for component in fusion.components:
        configured = next(
            (c for c in judgment.fusion.components if c.sensor_id == component.sensor_id), None
        )
        if configured is None or anchors[component.sensor_id].scan_id != component.anchor_scan_id:
            return "SECTION_FUSION_CONTEXT_MISMATCH"
        ratio = section_ratios[component.sensor_id]
        calibrated = None
        for left, right in zip(configured.calibration, configured.calibration[1:], strict=False):
            if left[0] <= ratio <= right[0]:
                calibrated = left[1] + (ratio - left[0]) * (right[1] - left[1]) / (
                    right[0] - left[0]
                )
                break
        if (
            calibrated is None
            or configured.weight != component.weight
            or not math.isclose(ratio, component.section_ratio, abs_tol=1e-9, rel_tol=0)
            or not math.isclose(calibrated, component.calibrated_ratio, abs_tol=1e-9, rel_tol=0)
        ):
            return "SECTION_FUSION_CONFIGURATION_MISMATCH"
    return None


async def read_section_frame(
    db: AsyncSession, source: MonitoringSource, now: datetime, status: DeviceStatus | None = None
) -> SectionFrame:
    settings = get_settings()
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
    status = status or await device_status(db, source.site_id, source.edge_id, now)
    device = next(
        d
        for d in settings.EDGE_V2_DEVICES
        if (d.site_id, d.edge_id) == (source.site_id, source.edge_id)
    )
    anchors = await scan_anchors(db, assessment, device.sensor_ids) if assessment else {}
    sections = {s.sensor_id: s for s in assessment.section_profiles or []} if assessment else {}
    page = LidarProfilePageV2(pitId=source.pit_id, profiles=[])
    ratios: dict[str, float] = {}
    reasons: list[str] = []
    for sid in device.sensor_ids:
        value = project_section(
            sid,
            sections.get(sid),
            assessment,
            configuration,
            reference,
            anchors.get(sid),
            status,
            now,
            settings.EDGE_V2_ASSESSMENT_TTL_SECONDS,
        )
        anchor = anchors.get(sid)
        section = sections.get(sid)
        depths = (
            {
                (b.ceiling_z_mm - b.floor_z_mm) / 10
                for b in section.profile.bins
                if b.source != "MASKED"
            }
            if section and section.profile
            else set()
        )
        heights = [
            s["height"] for s in value.curve.samples if s["height"] is not None and s["valid"]
        ]
        profile = LidarProfileV2(
            sensorId=sid,
            measuredAt=anchor.acquired_at if anchor else None,
            configRevision=assessment.config_revision if assessment else None,
            calibrationVersion=reference.calibration_versions.get(sid) if reference else None,
            sampleSource=None,
            sectionSource="EDGE_SECTION_PROFILE",
            sectionJudgmentState=value.wire_state,
            sourceScanId=anchor.scan_id if anchor else None,
            referenceDistanceCm=settings.EDGE_V2_EMPTY_FLOOR_DISTANCE_CM,
            referenceDepthCm=next(iter(depths)) if len(depths) == 1 else None,
            sectionLengthCm=value.curve.section_length_cm,
            simulated=reference is not None,
            timeBasis="SIMULATION_SOURCE_UTC_UNVERIFIED" if reference else "SOURCE_UTC",
            calibrationState="SIMULATION" if value.curve.samples else "UNAVAILABLE",
            samples=[
                LidarProfileSampleV2(
                    positionRatio=s["positionRatio"],
                    height=s["height"],
                    valid=s["valid"],
                    sourceIndex=s["sourceIndex"],
                    binSource=s["binSource"],
                )
                for s in value.curve.samples
            ],
            average=math.fsum(heights) / len(heights) if heights else None,
            minimum=min(heights) if heights else None,
            maximum=max(heights) if heights else None,
            reasonCodes=list(value.reason_codes),
            coverageRatio=section.profile.coverage_ratio
            if section and section.profile and value.curve.samples
            else 0,
            sectionFillPercent=value.area_ratio * 100 if value.area_ratio is not None else None,
        )
        if value.area_ratio is not None:
            ratios[sid] = value.area_ratio
        reasons.extend(value.curve.reason_codes)
        page.profiles.append(profile)
    if assessment is None or row is None:
        page.meanSectionReasonCodes = ["SECTION_STATE_NOT_RECEIVED"]
        return SectionFrame(page, None, False)
    error = fusion_error(assessment, configuration, anchors, ratios)
    if error:
        reasons.append(error)
    fusion = assessment.fusion_result
    raw = fusion.ratio * 100 if fusion and not reasons else None
    page.rawMeanSectionFillPercent = raw
    judgment = configuration.judgment if configuration else None
    verified = bool(
        judgment
        and judgment.verification_state == "VERIFIED"
        and judgment.fusion
        and judgment.fusion.verification_state == "VERIFIED"
    )
    available = (
        raw is not None
        and verified
        and assessment.decision_state == "AVAILABLE"
        and all(section.state == "AVAILABLE" for section in sections.values())
    )
    if not available and raw is not None:
        reasons.append("SECTION_JUDGMENT_LIMITED")
    observed = max((a.acquired_at for a in anchors.values()), default=None)
    if (
        observed is None
        or assessment.observed_to is None
        or assessment.observed_from is None
        or (
            observed > assessment.observed_to
            or assessment.observed_to > assessment.generated_at
            or min(a.acquired_at for a in anchors.values()) < assessment.observed_from
        )
    ):
        reasons.append("SECTION_OBSERVATION_TIME_MISMATCH")
        available = False
        raw = None
        page.rawMeanSectionFillPercent = None
    scope = digest(
        [
            reference_payload(reference) if reference else None,
            judgment.model_dump(mode="json") if judgment else None,
            settings.EDGE_V2_MEDIAN_WINDOW_SECONDS,
            [[sid, a.stream_instance_id, a.clock_domain_id] for sid, a in sorted(anchors.items())],
        ]
    )
    reasons = list(
        dict.fromkeys(
            [
                *assessment.reason_codes,
                *reasons,
                "EDGE_SECTION_FUSION_INPUT",
                *(
                    ["SIMULATION_GEOMETRY", "SIMULATION_SOURCE_CLOCK_UNVERIFIED"]
                    if reference
                    else []
                ),
            ]
        )
    )
    method = "EDGE_SECTION_FUSION_V1:" + scope
    observation = FillObservation(
        source="EDGE_STATE",
        simulated=reference is not None,
        time_basis="SIMULATION_SOURCE_UTC_UNVERIFIED" if reference else "SOURCE_UTC",
        site_id=source.site_id,
        edge_id=source.edge_id,
        config_revision=assessment.config_revision,
        message_id=assessment.message_id,
        observation_id=assessment.assessment_id,
        generated_at=assessment.generated_at,
        received_at=utc(row.received_at),
        observed_to=observed,
        rule_version=assessment.rule_version,
        method_version=method,
        scope_token=scope,
        value_percent=raw,
        data_state="AVAILABLE" if available else "LIMITED" if raw is not None else "UNAVAILABLE",
        reason_codes=reasons,
    )
    page.meanSectionReasonCodes = reasons if not available else []
    retain = (
        error == "SECTION_PAIR_UNMATCHED"
        and verified
        and len(ratios) == 2
        and all(section.state == "AVAILABLE" for section in sections.values())
    )
    return SectionFrame(page, observation, retain)


async def section_snapshot(
    db: AsyncSession,
    source: MonitoringSource,
    now: datetime,
    status: DeviceStatus | None = None,
    pit: PitConfig | None = None,
) -> tuple[
    SectionFrame,
    DashboardDataState,
    list[str],
    float | None,
    float | None,
    Literal["READY", "WARMING", "UNAVAILABLE"],
]:
    frame = await read_section_frame(db, source, now, status)
    observation = frame.observation
    if observation is None:
        return frame, "NO_DATA", frame.page.meanSectionReasonCodes, None, None, "UNAVAILABLE"
    reasons = list(observation.reason_codes)
    if observation.data_state != "AVAILABLE":
        return frame, "UNAVAILABLE", reasons, None, None, "UNAVAILABLE"
    pit = pit or await db.get(PitConfig, source.pit_id)
    threshold = pit.critical_threshold_percent if pit else get_settings().EDGE_V2_CRITICAL_THRESHOLD
    row = await db.get(EdgeV2Filter, (source.site_id, source.edge_id))
    if row is None or any(
        (
            row.payload.get("message_id") != observation.message_id,
            row.payload.get("methodVersion") != observation.method_version,
            row.payload.get("inputSource") != observation.source,
            row.payload.get("threshold") != threshold,
        )
    ):
        return (
            frame,
            "UNKNOWN",
            [*reasons, "FILTER_UNAVAILABLE"],
            observation.value_percent,
            None,
            "UNAVAILABLE",
        )
    if row.payload.get("state") == "READY" and row.payload.get("value") is not None:
        value = float(row.payload["value"])
        frame.page.meanSectionFillPercent = value
        return frame, "FRESH", reasons, observation.value_percent, value, "READY"
    return (
        frame,
        "UNKNOWN",
        [*reasons, "FILTER_WARMING"],
        observation.value_percent,
        None,
        "WARMING",
    )


async def project_section_input(
    db: AsyncSession, assessment: Assessment, receipt: EdgeV2Message, row: EdgeV2Filter
) -> None:
    from src.app.services.edge_v2_projection import project_input

    source = next(
        (
            s
            for s in get_settings().EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (assessment.site_id, assessment.edge_id)
        ),
        None,
    )
    if source is None:
        row.payload = {}
        return
    frame = await read_section_frame(db, source, utc(receipt.received_at))
    if frame.observation is None:
        row.payload = {}
        return
    await project_input(
        db,
        frame.observation,
        receipt,
        row,
        section_mode=True,
        retain_section_history=frame.retain_pair_history,
    )
