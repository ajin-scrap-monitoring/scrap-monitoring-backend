"""Indexed time-range query and SQL reduction before loading historical STATE payloads."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Minute
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
            message.compact.is_(False),
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
    compact = await compact_samples(db, source, start, end, seconds)
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
        retained = compact.get(index)
        if retained and (sample.generatedAt is None or retained[0] > sample.generatedAt):
            sample = HistorySampleV2(
                bucketStartAt=sample.bucketStartAt,
                bucketEndAt=sample.bucketEndAt,
                **retained[1],
            )
        samples.append(sample)
    return LoadHistoryV2(
        pitId=source.pit_id,
        source=source,
        from_time=start,
        to_time=end,
        bucketSeconds=seconds,
        samples=samples,
    )


async def compact_samples(
    db: AsyncSession, source: MonitoringSource, start: datetime, end: datetime, seconds: int
) -> dict[int, tuple[datetime, dict[str, Any]]]:
    """Bounded seven-day query of minute rows; no periodic STATE or point arrays needed."""
    rows = await db.scalars(
        select(EdgeV2Minute)
        .where(
            EdgeV2Minute.site_id == source.site_id,
            EdgeV2Minute.edge_id == source.edge_id,
            EdgeV2Minute.bucket_start >= start.replace(second=0, microsecond=0),
            EdgeV2Minute.bucket_start < end,
        )
        .order_by(EdgeV2Minute.bucket_start)
    )
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        for stamp, value in row.payload.get("values", {}).items():
            timestamp = datetime.fromtimestamp(int(stamp), UTC)
            # Scalar slots have only second resolution; do not invent subsecond metadata.
            if not start <= timestamp < end:
                continue
            key = int(timestamp.timestamp() // seconds)
            group = grouped.setdefault(
                key,
                {
                    "values": [],
                    "invalid": 0,
                    "sources": set(),
                    "methods": set(),
                    "simulated": False,
                },
            )
            slot_source = row.payload.get("sources", {}).get(stamp, "EDGE_STATE")
            slot_method = row.payload.get("methods", {}).get(stamp)
            if str(slot_method).startswith("SIMULATION_HEIGHT_SECTION_V1:"):
                group["simulated"] = True
            if value is not None:
                group["values"].append(value)
                group["sources"].add(slot_source)
                if slot_method is not None:
                    group["methods"].add(slot_method)
            else:
                group["invalid"] += 1
            if "last_time" not in group or timestamp >= group["last_time"]:
                group.update(
                    last_time=timestamp,
                    value=value,
                    last=row.payload["last"],
                    last_source=slot_source,
                    last_method=slot_method,
                )
    result: dict[int, tuple[datetime, dict[str, Any]]] = {}
    for key, group in grouped.items():
        last = group["last"]
        generated = datetime.fromisoformat(last["generatedAt"])
        metadata_retained = start <= generated < end and int(generated.timestamp()) == int(
            group["last_time"].timestamp()
        )
        values = group["values"]
        value = group["value"]
        mixed = len(group["sources"]) > 1 or len(group["methods"]) > 1
        input_source = next(iter(group["sources"]), group["last_source"]) if not mixed else "MIXED"
        if mixed:
            values, value = [], None
        data = {
            "generatedAt": generated if metadata_retained else None,
            "observedAt": last["observedAt"] if metadata_retained else None,
            "assessmentId": last["assessmentId"] if metadata_retained else None,
            "observationId": last.get("observationId") if metadata_retained else None,
            "inputSource": input_source,
            "simulated": group["simulated"],
            "methodVersion": next(iter(group["methods"]), group["last_method"])
            if not mixed
            else None,
            "valuePercent": value,
            "valid": value is not None,
            "dataState": "UNKNOWN"
            if mixed
            else last["dataState"]
            if metadata_retained
            else ("AVAILABLE" if value is not None else "UNKNOWN"),
            "reasonCodes": ["FILL_METHOD_CHANGED_WITHIN_BUCKET"]
            if mixed
            else last["reasonCodes"]
            if metadata_retained
            else ["COMPACT_SUBMINUTE_METADATA_NOT_RETAINED"],
            "averagePercent": sum(values) / len(values) if values else None,
            "minimumPercent": min(values) if values else None,
            "maximumPercent": max(values) if values else None,
            "validSamples": len(values),
            "invalidSamples": group["invalid"] + len(group["values"])
            if mixed
            else group["invalid"],
            "valuePolicy": "MEDIAN_MINUTE",
        }
        result[key] = (generated if metadata_retained else group["last_time"], data)
    return result
