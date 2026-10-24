"""A bounded recent trend, reset by missing data, method changes and collection drops."""

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.schemas.fill_forecast import FillForecastV2
from src.app.schemas.load_history_v2 import HistorySampleV2
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.collection_summary_v2_service import collection_summary
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import build_snapshot

WINDOW_SECONDS = 600
MAX_GAP_SECONDS = 90
MIN_SPAN_SECONDS = 60
MIN_SAMPLES = 3
DROP_PERCENT = 1.0
MIN_R_SQUARED = 0.8
MAX_REMAINING_SECONDS = 24 * 3600


def recent_points(
    samples: list[HistorySampleV2],
    input_source: str,
    method_version: str | None,
    simulated: bool,
    cutoff: datetime,
    now: datetime,
) -> list[tuple[datetime, float]]:
    """Keep a contiguous, compatible suffix; never turn missing values into zero."""
    points: list[tuple[datetime, float]] = []
    for sample in samples:
        observed, generated, value = sample.observedAt, sample.generatedAt, sample.valuePercent
        if (
            not sample.valid
            or sample.invalidSamples > 0
            or sample.dataState != "AVAILABLE"
            or value is None
            or observed is None
            or generated is None
            or not cutoff <= observed <= generated <= now
            or sample.inputSource != input_source
            or sample.methodVersion != method_version
            or sample.simulated != simulated
        ):
            points = []
            continue
        if points:
            gap = (observed - points[-1][0]).total_seconds()
            if gap <= 0 or gap > MAX_GAP_SECONDS or value < points[-1][1] - DROP_PERCENT:
                points = []
        points.append((observed, value))
    return points


def project_trend(result: FillForecastV2, points: list[tuple[datetime, float]]) -> None:
    """Fit percent against actual elapsed seconds; do not infer a guaranteed arrival."""
    result.sampleCount = len(points)
    if len(points) < MIN_SAMPLES:
        result.reasonCodes = ["INSUFFICIENT_TREND_SAMPLES"]
        return
    result.windowFrom, result.windowTo = points[0][0], points[-1][0]
    if (result.windowTo - result.windowFrom).total_seconds() < MIN_SPAN_SECONDS:
        result.reasonCodes = ["INSUFFICIENT_TREND_SPAN"]
        return
    xs = [(at - points[0][0]).total_seconds() for at, _ in points]
    ys = [value for _, value in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    xx = sum((x - mean_x) ** 2 for x in xs)
    yy = sum((y - mean_y) ** 2 for y in ys)
    xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = xy / xx
    result.increasePercentPerMinute = slope * 60
    if slope <= 0 or yy == 0:
        result.reasonCodes = ["LOAD_NOT_INCREASING"]
        return
    result.trendRSquared = min(1.0, max(0.0, xy * xy / (xx * yy)))
    if result.trendRSquared < MIN_R_SQUARED:
        result.reasonCodes = ["UNSTABLE_LOAD_TREND"]
        return
    assert result.currentLoadPercent is not None
    remaining = (result.targetThresholdPercent - result.currentLoadPercent) / slope
    if remaining > MAX_REMAINING_SECONDS:
        result.reasonCodes = ["FORECAST_HORIZON_EXCEEDED"]
        return
    result.remainingSeconds = max(0, math.ceil(remaining - 1e-9))
    result.expectedThresholdAt = result.computedAt + timedelta(seconds=result.remainingSeconds)
    result.state = "AVAILABLE"


async def fill_forecast(
    db: AsyncSession, source: MonitoringSource, now: datetime | None = None
) -> FillForecastV2:
    now = now or datetime.now(UTC)
    snapshot = await build_snapshot(db, source, now)
    current = snapshot.summary.loadPercent
    result = FillForecastV2(
        pitId=source.pit_id,
        computedAt=now,
        validUntil=now,
        simulated=snapshot.summary.simulated,
        inputSource=snapshot.summary.inputSource,
        currentLoadPercent=current,
        targetThresholdPercent=snapshot.summary.collectionThresholdPercent,
        windowFrom=now - timedelta(seconds=WINDOW_SECONDS),
        windowTo=now,
    )
    if snapshot.dataState != "FRESH" or current is None or snapshot.measuredAt is None:
        result.reasonCodes = ["CURRENT_LOAD_UNAVAILABLE", *snapshot.reasonCodes]
        return result
    result.validUntil = min(
        now + timedelta(seconds=60),
        snapshot.measuredAt + timedelta(seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS),
    )
    if current >= result.targetThresholdPercent:
        result.state, result.expectedThresholdAt, result.remainingSeconds = "REACHED", now, 0
        result.reasonCodes = ["THRESHOLD_ALREADY_REACHED"]
        return result
    collections = await collection_summary(db, source, now=now)
    if collections.detectionEnabled and collections.detectionState in ("CANDIDATE", "COLLECTING"):
        result.reasonCodes = ["COLLECTION_IN_PROGRESS"]
        return result
    if collections.detectionEnabled and collections.detectionState == "UNKNOWN":
        result.reasonCodes = ["COLLECTION_STATE_UNAVAILABLE"]
        return result
    cutoff = max(
        at for at in (result.windowFrom, collections.lastCollectionActivityAt) if at is not None
    )
    # Include a measurement exactly at `now` without changing the history API's [from,to) rule.
    history = await load_history(
        db, source, result.windowFrom, now + timedelta(microseconds=1), 60, now
    )
    if any(
        sample.observedAt is not None and sample.observedAt > snapshot.measuredAt
        for sample in history.samples
    ):
        result.reasonCodes = ["OBSERVATION_CHANGED_DURING_FORECAST"]
        return result
    method = (
        snapshot.fillObservation.method_version
        if snapshot.fillObservation
        else snapshot.assessment.fill_estimate.method_version
        if snapshot.assessment and snapshot.assessment.fill_estimate
        else None
    )
    points = recent_points(
        history.samples, result.inputSource, method, result.simulated, cutoff, now
    )
    # The current partial minute must not retain the previous minute's trend through a drop.
    if points and (
        (snapshot.measuredAt - points[-1][0]).total_seconds() > MAX_GAP_SECONDS
        or current < points[-1][1] - DROP_PERCENT
    ):
        points = []
    if cutoff <= snapshot.measuredAt <= now and (not points or snapshot.measuredAt > points[-1][0]):
        points.append((snapshot.measuredAt, current))
    project_trend(result, points)
    return result
