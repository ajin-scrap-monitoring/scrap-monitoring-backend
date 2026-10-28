"""Indexed time-range query and SQL reduction before loading historical STATE payloads."""

import asyncio
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import extract, func, select, text
from sqlalchemy.exc import DBAPIError
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
from src.app.services.compact_history_queries import compact_aggregate_query
from src.app.services.compact_history_stream import compact_stream_groups

MAX_POINTS = 1441
HISTORY_QUERY_TIMEOUT_MILLISECONDS = 20000
INTERVALS: tuple[HistoryInterval, ...] = (60, 300, 900, 3600, 7200, 21600, 86400)


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
    if not timedelta(0) < end - start <= timedelta(days=90):
        raise HTTPException(422, "HISTORY_RANGE_MUST_BE_WITHIN_90_DAYS")
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
    if db.get_bind().dialect.name == "postgresql":
        # Request-local settings reset on commit/rollback, never changing the DB globally.
        await db.execute(
            text(
                "SELECT set_config('jit', 'off', true), "
                "set_config('statement_timeout', :budget, true)"
            ),
            {"budget": str(HISTORY_QUERY_TIMEOUT_MILLISECONDS)},
        )
    try:
        if db.get_bind().dialect.name == "postgresql":
            async with asyncio.timeout(HISTORY_QUERY_TIMEOUT_MILLISECONDS / 1000):
                return await _read_history(db, source, start, end, seconds, now)
        return await _read_history(db, source, start, end, seconds, now)
    except TimeoutError:
        raise HTTPException(503, "HISTORY_QUERY_TIMEOUT") from None
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014":
            raise HTTPException(503, "HISTORY_QUERY_TIMEOUT") from None
        raise


async def _read_history(
    db: AsyncSession,
    source: MonitoringSource,
    start: datetime,
    end: datetime,
    seconds: HistoryInterval,
    now: datetime,
) -> LoadHistoryV2:
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
            sample.methodVersion = (
                assessment.fill_estimate.method_version if assessment.fill_estimate else None
            )
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
    """Bounded history without retaining all minute payloads in application memory."""
    if db.get_bind().dialect.name == "postgresql":
        return _compact_result(
            await compact_stream_groups(db, source, start, end, seconds), start, end
        )
    start_minute = start.replace(second=0, microsecond=0)
    end_minute = end.replace(second=0, microsecond=0)
    whole_start = start_minute + timedelta(minutes=1) if start != start_minute else start
    grouped: dict[int, dict[str, Any]] = {}
    aggregate = await db.execute(
        compact_aggregate_query(db, source, whole_start, end_minute, seconds)
    )
    for summary in aggregate.mappings():
        metadata, fallback = summary["metadata"], summary["fallback"]
        # PostgreSQL returns JSON objects; SQLite's JSON functions return encoded text.
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
        fallback = json.loads(fallback) if isinstance(fallback, str) else fallback
        timestamp = datetime.fromtimestamp(summary["stamp"], UTC)
        generated = datetime.fromisoformat(metadata["generatedAt"]) if metadata else None
        precise = generated is not None and int(generated.timestamp()) == summary["stamp"]
        grouped[int(summary["bucket"])] = {
            "total": summary["total"] or 0.0,
            "count": summary["count"],
            "invalid": summary["invalid"],
            "minimum": summary["minimum"],
            "maximum": summary["maximum"],
            "sources": {summary["source"]} if summary["source"] is not None else set(),
            "methods": {summary["method"]} if summary["method"] is not None else set(),
            "mixed": summary["source_count"] > 1 or summary["method_count"] > 1,
            "simulated": bool(summary["simulated"]),
            "last_time": generated if precise else timestamp,
            "value": summary["value"],
            "last": metadata if precise else fallback,
            "last_source": summary["last_source"] or "EDGE_STATE",
            "last_method": summary["last_method"],
        }
    boundaries = set()
    if start != start_minute:
        boundaries.add(start_minute)
    if end != end_minute:
        boundaries.add(end_minute)
    rows: Sequence[EdgeV2Minute] = ()
    if boundaries:
        rows = (
            await db.scalars(
                select(EdgeV2Minute)
                .where(
                    EdgeV2Minute.site_id == source.site_id,
                    EdgeV2Minute.edge_id == source.edge_id,
                    EdgeV2Minute.bucket_start.in_(boundaries),
                )
                .order_by(EdgeV2Minute.bucket_start)
            )
        ).all()
    for row in rows:
        for stamp, value in row.payload.get("values", {}).items():
            timestamp = datetime.fromtimestamp(int(stamp), UTC)
            metadata = row.payload.get("metadata", {}).get(stamp)
            generated = datetime.fromisoformat(metadata["generatedAt"]) if metadata else None
            precise = generated is not None and int(generated.timestamp()) == int(stamp)
            if precise:
                assert generated is not None
                timestamp = generated
            # Legacy scalar keys have only second resolution; never invent precise times.
            # New slots carry their own true time; legacy slots keep the old null policy.
            if not start <= timestamp < end:
                continue
            key = int(timestamp.timestamp() // seconds)
            group = grouped.setdefault(
                key,
                {
                    "total": 0.0,
                    "count": 0,
                    "minimum": None,
                    "maximum": None,
                    "invalid": 0,
                    "sources": set(),
                    "methods": set(),
                    "simulated": False,
                    "mixed": False,
                },
            )
            slot_source = row.payload.get("sources", {}).get(stamp, "EDGE_STATE")
            slot_method = row.payload.get("methods", {}).get(stamp)
            if str(slot_method).startswith(
                ("SIMULATION_HEIGHT_SECTION_V1:", "SIMULATION_SENSOR_ANGLE_V1:")
            ):
                group["simulated"] = True
            if value is not None:
                group["total"] += value
                group["count"] += 1
                group["minimum"] = (
                    min(group["minimum"], value) if group["minimum"] is not None else value
                )
                group["maximum"] = (
                    max(group["maximum"], value) if group["maximum"] is not None else value
                )
                group["sources"].add(slot_source)
                if slot_method is not None:
                    group["methods"].add(slot_method)
            else:
                group["invalid"] += 1
            if "last_time" not in group or timestamp >= group["last_time"]:
                group.update(
                    last_time=timestamp,
                    value=value,
                    last=metadata if precise else row.payload["last"],
                    last_source=slot_source,
                    last_method=slot_method,
                )
    return _compact_result(grouped, start, end)


def _compact_result(
    grouped: dict[int, dict[str, Any]], start: datetime, end: datetime
) -> dict[int, tuple[datetime, dict[str, Any]]]:
    result: dict[int, tuple[datetime, dict[str, Any]]] = {}
    for key, group in grouped.items():
        last = group["last"]
        generated = datetime.fromisoformat(last["generatedAt"])
        metadata_retained = start <= generated < end and int(generated.timestamp()) == int(
            group["last_time"].timestamp()
        )
        count = group["count"]
        value = group["value"]
        mixed = group["mixed"] or len(group["sources"]) > 1 or len(group["methods"]) > 1
        input_source = next(iter(group["sources"]), group["last_source"]) if not mixed else "MIXED"
        if mixed:
            value = None
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
            "averagePercent": group["total"] / count if count and not mixed else None,
            "minimumPercent": group["minimum"] if not mixed else None,
            "maximumPercent": group["maximum"] if not mixed else None,
            "validSamples": count if not mixed else 0,
            "invalidSamples": group["invalid"] + count if mixed else group["invalid"],
            "valuePolicy": "MEDIAN_MINUTE",
        }
        result[key] = (generated if metadata_retained else group["last_time"], data)
    return result
