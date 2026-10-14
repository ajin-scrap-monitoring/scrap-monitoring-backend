"""Indexed time-range query and SQL reduction before loading historical STATE payloads."""

import math
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message
from src.app.schemas.edge_v2 import Assessment
from src.app.schemas.load_history_v2 import (
    HistoryDataState,
    HistoryInterval,
    HistorySampleV2,
    LoadHistoryV2,
)
from src.app.schemas.monitoring_v2 import MonitoringSource

MAX_POINTS = 1441
INTERVALS: tuple[HistoryInterval, ...] = (60, 300, 900, 3600)


def history_window(
    from_time: datetime | None,
    to_time: datetime | None,
    interval: HistoryInterval | None,
    now: datetime,
) -> tuple[datetime, datetime, HistoryInterval]:
    for value in (from_time, to_time):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(422, "HISTORY_TIMEZONE_REQUIRED")
    try:
        end = to_time.astimezone(UTC) if to_time else now
        start = from_time.astimezone(UTC) if from_time else end - timedelta(hours=24)
    except (OverflowError, ValueError):
        raise HTTPException(422, "HISTORY_TIME_OUT_OF_RANGE") from None
    if end > now + timedelta(seconds=5):
        raise HTTPException(422, "HISTORY_FUTURE_RANGE")
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise HTTPException(422, "HISTORY_RANGE_MUST_BE_WITHIN_7_DAYS")
    choices = (interval,) if interval is not None else INTERVALS
    for seconds in choices:
        points = math.ceil(end.timestamp() / seconds) - math.floor(start.timestamp() / seconds)
        if points <= MAX_POINTS:
            return start, end, seconds
    raise HTTPException(422, "HISTORY_TOO_MANY_POINTS")


def historical_value(
    assessment: Assessment,
    now: datetime,
) -> tuple[float | None, HistoryDataState, list[str]]:
    """Historical validity uses source generation time, never today's status/arrival time."""
    reasons = list(assessment.reason_codes)
    observed = assessment.observed_to
    value: float | None = None
    state: HistoryDataState
    if assessment.generated_at > now + timedelta(seconds=5) or (
        observed is not None and observed > assessment.generated_at
    ):
        state = "UNKNOWN"
        reasons.append("ASSESSMENT_CLOCK_SKEW")
    elif observed is not None and assessment.generated_at - observed >= timedelta(
        seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS
    ):
        state = "STALE"
        reasons.append("ASSESSMENT_STALE_AT_GENERATION")
    elif assessment.fill_estimate is None or assessment.decision_state == "UNAVAILABLE":
        state = "UNAVAILABLE"
    elif observed is None:
        state = "UNKNOWN"
        reasons.append("OBSERVATION_TIME_UNAVAILABLE")
    else:
        value = round(assessment.fill_estimate.ratio * 100, 6)
        state = "LIMITED" if assessment.decision_state == "LIMITED" else "AVAILABLE"
    return value, state, list(dict.fromkeys(reasons))


async def load_history(
    db: AsyncSession,
    source: MonitoringSource,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    interval: HistoryInterval | None = None,
    now: datetime | None = None,
) -> LoadHistoryV2:
    now = now or datetime.now(UTC)
    start, end, seconds = history_window(from_time, to_time, interval, now)
    message = EdgeV2Message
    bucket = func.floor(extract("epoch", message.generated_at) / seconds)
    ranked = (
        select(
            message.id,
            bucket.label("bucket"),
            func.row_number()
            .over(partition_by=bucket, order_by=(message.generated_at.desc(), message.id.asc()))
            .label("rank"),
        )
        .where(
            message.site_id == source.site_id,
            message.edge_id == source.edge_id,
            message.message_type == "edge_assessment",
            message.generated_at >= start,
            message.generated_at < end,
            message.payload["assessment_type"].as_string() == "STATE",
        )
        .subquery()
    )
    # Sort only IDs/times in SQL. At most MAX_POINTS full STATE payloads reach Python.
    rows = await db.execute(
        select(message.payload, ranked.c.bucket)
        .join(ranked, ranked.c.id == message.id)
        .where(ranked.c.rank == 1)
        .order_by(ranked.c.bucket)
    )
    states = {int(row.bucket): Assessment.model_validate(row.payload) for row in rows}
    first = math.floor(start.timestamp() / seconds)
    stop = math.ceil(end.timestamp() / seconds)
    samples: list[HistorySampleV2] = []
    for index in range(first, stop):
        bucket_start = datetime.fromtimestamp(index * seconds, UTC)
        sample = HistorySampleV2(
            bucketStartAt=max(start, bucket_start),
            bucketEndAt=min(end, bucket_start + timedelta(seconds=seconds)),
        )
        assessment = states.get(index)
        if assessment:
            value, state, reasons = historical_value(assessment, now)
            sample.generatedAt = assessment.generated_at
            sample.observedAt = assessment.observed_to
            sample.assessmentId = assessment.assessment_id
            sample.valuePercent = value
            sample.valid = value is not None
            sample.dataState = state
            sample.reasonCodes = reasons
        samples.append(sample)
    return LoadHistoryV2(
        pitId=source.pit_id,
        source=source,
        from_time=start,
        to_time=end,
        bucketSeconds=seconds,
        samples=samples,
    )
