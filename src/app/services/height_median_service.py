"""Bounded temporal medians of verified height bins, preserving the existing cm fields."""

from datetime import datetime, timedelta
from statistics import fmean, median
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Measurement, StatusReport, UtcTime
from src.app.schemas.monitoring_v2 import LidarProfilePageV2, LidarProfileV2, MonitoringSource
from src.app.services.edge_v2_service import device_status, digest, utc
from src.app.services.height_reference import (
    device_clock_allowed,
    height_reference,
    reference_depth,
)


class HeightFrame(BaseModel):
    at: UtcTime
    heights: list[float | None] = Field(max_length=200)


class SensorWindow(BaseModel):
    positions: list[float] = Field(default_factory=list, max_length=200)
    frames: list[HeightFrame] = Field(default_factory=list, max_length=31)


class HeightWindow(BaseModel):
    scope: str
    config_revision: str
    sensors: dict[str, SensorWindow]


def height_median_enabled() -> bool:
    settings = get_settings()
    return settings.EDGE_V2_HEIGHT_MEDIAN_ENABLED or settings.EDGE_V2_HEIGHT_FILL_ENABLED


async def window_scope(db: AsyncSession, source: MonitoringSource, now: datetime) -> str:
    settings = get_settings()
    reference = height_reference(source.pit_id)
    pit = await db.get(PitConfig, source.pit_id)
    status = await device_status(db, source.site_id, source.edge_id, now)
    return digest(
        [
            source.pit_id,
            reference.model_dump(mode="json") if reference else None,
            pit.total_depth_cm if pit else None,
            pit.critical_threshold_percent if pit else settings.EDGE_V2_CRITICAL_THRESHOLD,
            settings.EDGE_V2_MEDIAN_WINDOW_SECONDS,
            status.snapshot.orchestrator_instance_id if status.snapshot else None,
        ]
    )


def advance(profile: LidarProfileV2, previous: SensorWindow) -> SensorWindow:
    if (
        profile.calibrationState not in ("VERIFIED", "SIMULATION")
        or profile.measuredAt is None
        or not profile.samples
    ):
        return SensorWindow()
    size = get_settings().EDGE_V2_MEDIAN_WINDOW_SECONDS
    positions = [float(cast(float, s["positionRatio"])) for s in profile.samples]
    heights = [
        float(s["height"]) if s["valid"] and s["height"] is not None else None
        for s in profile.samples
    ]
    frames = previous.frames if previous.positions == positions else []
    at = profile.measuredAt
    if frames and at <= frames[-1].at:
        # A repeated scan can be displayed, but never counts as an additional observation.
        if at == frames[-1].at and heights == frames[-1].heights:
            return previous
        return SensorWindow(positions=positions)
    if frames and at - frames[-1].at > timedelta(seconds=1.5):
        frames = []
    frames = [
        f
        for f in frames
        if at - f.at < timedelta(seconds=size) and int(f.at.timestamp()) != int(at.timestamp())
    ]
    return SensorWindow(
        positions=positions, frames=[*frames, HeightFrame(at=at, heights=heights)][-size:]
    )


def filtered_page(
    page: LidarProfilePageV2, window: HeightWindow | None, depth: float | None
) -> LidarProfilePageV2:
    result = page.model_copy(deep=True)
    size = get_settings().EDGE_V2_MEDIAN_WINDOW_SECONDS
    reference = height_reference(page.pitId)
    depth = reference_depth(reference, depth)
    for profile in result.profiles:
        profile.valuePolicy = "SPATIAL_TEMPORAL_MEDIAN"
        profile.average = profile.minimum = profile.maximum = profile.sectionFillPercent = None
        profile.coverageRatio = 0
        history = window.sensors.get(profile.sensorId) if window else None
        positions = [s["positionRatio"] for s in profile.samples]
        ready = (
            history is not None
            and len(history.frames) == size
            and history.frames[-1].at == profile.measuredAt
            and history.positions == positions
            and profile.calibrationState in ("VERIFIED", "SIMULATION")
        )
        values: list[float] = []
        for index, sample in enumerate(profile.samples):
            heights = [f.heights[index] for f in history.frames] if ready and history else []
            value = (
                float(median(float(v) for v in heights if v is not None))
                if (ready and sample["valid"] and all(v is not None for v in heights))
                else None
            )
            sample.update(height=value, valid=value is not None)
            if value is not None:
                values.append(value)
        if values:
            profile.average, profile.minimum, profile.maximum = (
                fmean(values),
                min(values),
                max(values),
            )
            profile.coverageRatio = len(values) / len(profile.samples)
        if reference and depth and profile.coverageRatio >= reference.minimum_coverage and values:
            profile.sectionFillPercent = fmean(values) / depth * 100
        if (
            profile.calibrationState in ("VERIFIED", "SIMULATION")
            and profile.sectionFillPercent is None
        ):
            profile.reasonCodes = list(
                dict.fromkeys([*profile.reasonCodes, "HEIGHT_TEMPORAL_WARMING_OR_MISSING"])
            )
    result.meanSectionFillPercent = None
    ratios = [p.sectionFillPercent for p in result.profiles if p.sectionFillPercent is not None]
    if len(ratios) == 2 and not page.meanSectionReasonCodes:
        result.meanSectionFillPercent = fmean(ratios)
    elif not result.meanSectionReasonCodes:
        result.meanSectionReasonCodes = ["HEIGHT_TEMPORAL_WARMING_OR_MISSING"]
    return result


async def project_height_window(
    db: AsyncSession,
    source: MonitoringSource,
    measurement: Measurement,
    receipt: EdgeV2Message,
    page: LidarProfilePageV2,
) -> LidarProfilePageV2:
    key = (source.site_id, source.edge_id, "height_window")
    row = await db.get(EdgeV2Latest, key)
    scope = await window_scope(db, source, utc(receipt.received_at))
    previous = HeightWindow.model_validate(row.payload) if row and row.payload else None
    sensors = (
        previous.sensors
        if previous
        and previous.scope == scope
        and previous.config_revision == measurement.config_revision
        else {}
    )
    window = HeightWindow(
        scope=scope,
        config_revision=measurement.config_revision,
        sensors={
            p.sensorId: advance(p, sensors.get(p.sensorId, SensorWindow())) for p in page.profiles
        },
    )
    if row is None:
        row = EdgeV2Latest(
            site_id=source.site_id,
            edge_id=source.edge_id,
            kind="height_window",
            message_id=measurement.message_id,
            generated_at=measurement.generated_at,
            payload={},
        )
        db.add(row)
    row.message_id, row.generated_at, row.payload = (
        measurement.message_id,
        measurement.generated_at,
        window.model_dump(mode="json"),
    )
    pit = await db.get(PitConfig, source.pit_id)
    return filtered_page(page, window, pit.total_depth_cm if pit else None)


async def read_height_window(
    db: AsyncSession, source: MonitoringSource, page: LidarProfilePageV2, now: datetime
) -> LidarProfilePageV2:
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, "height_window"))
    latest = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, "measurement"))
    window = HeightWindow.model_validate(row.payload) if row and row.payload else None
    if (
        row is None
        or latest is None
        or row.message_id != latest.message_id
        or window
        and window.scope != await window_scope(db, source, now)
    ):
        window = None
    pit = await db.get(PitConfig, source.pit_id)
    return filtered_page(page, window, pit.total_depth_cm if pit else None)


async def invalidate_on_status(
    db: AsyncSession, status: StatusReport, now: datetime, same_instance: bool
) -> None:
    row = await db.get(EdgeV2Latest, (status.site_id, status.edge_id, "height_window"))
    if row is None:
        return
    if (
        not same_instance
        or status.overall_state not in ("HEALTHY", "DEGRADED")
        or not device_clock_allowed(status.site_id, status.edge_id, status.clock.state)
        or status.generated_at > now
        or row.payload
        and row.payload.get("config_revision") != status.config_revision
        or len(status.sensors) != 2
        or any(
            s.data_state != "FRESH"
            or s.last_scan_at is None
            or not timedelta(0)
            <= now - s.last_scan_at
            < timedelta(seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
            for s in status.sensors
        )
    ):
        row.payload = {}
