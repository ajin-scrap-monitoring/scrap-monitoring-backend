"""Bounded-memory compact history using stored minute statistics and scalar provenance."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.schemas.monitoring_v2 import MonitoringSource

# Reduce cursor round trips while keeping the decoded buffer bounded to one page.
STREAM_PAGE_SIZE = 512
SIMULATION_PREFIXES = ("SIMULATION_HEIGHT_SECTION_V1:", "SIMULATION_SENSOR_ANGLE_V1:")


def _group() -> dict[str, Any]:
    return {
        "total": 0.0,
        "count": 0,
        "invalid": 0,
        "minimum": None,
        "maximum": None,
        "sources": set(),
        "methods": set(),
        "mixed": False,
        "simulated": False,
    }


def _distinct(group: dict[str, Any], field: str, value: str) -> None:
    # Two distinct values already prove a mixed bucket; do not retain an unbounded set.
    if len(group[field]) < 2:
        group[field].add(value)


def _stats(
    group: dict[str, Any], count: int, invalid: int, total: float, low: Any, high: Any
) -> None:
    group["total"] += total
    group["count"] += count
    group["invalid"] += invalid
    if count:
        group["minimum"] = min(group["minimum"], low) if group["minimum"] is not None else low
        group["maximum"] = max(group["maximum"], high) if group["maximum"] is not None else high


def _slot_time(stamp: str, metadata: dict[str, Any]) -> tuple[datetime, dict[str, Any] | None]:
    timestamp = datetime.fromtimestamp(int(stamp), UTC)
    retained = metadata.get(stamp)
    generated = datetime.fromisoformat(retained["generatedAt"]) if retained else None
    if generated is not None and int(generated.timestamp()) == int(stamp):
        return generated, retained
    return timestamp, None


def _last(
    group: dict[str, Any],
    stamp: str,
    value: Any,
    data: dict[str, Any],
    sources: dict[str, Any],
    methods: dict[str, Any],
) -> None:
    timestamp, retained = _slot_time(stamp, data.get("metadata", {}))
    if "last_time" not in group or timestamp >= group["last_time"]:
        group.update(
            last_time=timestamp,
            value=value,
            last=retained if retained else data["last"],
            last_source=sources.get(stamp, "EDGE_STATE"),
            last_method=methods.get(stamp),
        )


def _provenance(group: dict[str, Any], stamp: str, value: Any, sources: Any, methods: Any) -> None:
    method = methods.get(stamp)
    if isinstance(method, str) and method.startswith(SIMULATION_PREFIXES):
        group["simulated"] = True
    if value is not None:
        _distinct(group, "sources", sources.get(stamp, "EDGE_STATE"))
        if method is not None:
            _distinct(group, "methods", method)


def _whole_minute(
    groups: dict[int, dict[str, Any]],
    bucket: int,
    data: dict[str, Any],
) -> None:
    values = data.get("values", {})
    if not values:
        return
    group = groups.setdefault(bucket, _group())
    sources, methods = data.get("sources", {}), data.get("methods", {})
    valid = []
    last_stamp = max(values, key=int)
    for stamp, value in values.items():
        _provenance(group, stamp, value, sources, methods)
        if value is not None:
            valid.append(value)
    # The writer atomically recalculates these statistics with each scalar replacement.
    # Legacy rows without the cache still have at most sixty scalar slots to reduce.
    cached = (
        data.get("valid_count") == len(valid)
        and data.get("invalid_count") == len(values) - len(valid)
        and isinstance(data.get("sum"), (int, float))
        and math.isfinite(data["sum"])
        and (
            not valid or all(isinstance(data.get(k), (int, float)) for k in ("minimum", "maximum"))
        )
    )
    _stats(
        group,
        len(valid),
        len(values) - len(valid),
        data["sum"] if cached else sum(valid),
        data.get("minimum") if cached else min(valid, default=None),
        data.get("maximum") if cached else max(valid, default=None),
    )
    _last(group, last_stamp, values[last_stamp], data, sources, methods)


async def compact_stream_groups(
    db: AsyncSession, source: MonitoringSource, start: datetime, end: datetime, seconds: int
) -> dict[int, dict[str, Any]]:
    """Stream one bounded page at a time, keeping only one accumulator per output bucket.

    Do not sort/project JSON in PostgreSQL: sorting wide JSON projections blocks the
    first page and spills to disk. Bucket accumulation is independent of read order.
    """
    minute = EdgeV2Minute
    statement = select(minute.bucket_start, minute.payload).where(
        minute.site_id == source.site_id,
        minute.edge_id == source.edge_id,
        minute.bucket_start >= start.replace(second=0, microsecond=0),
        minute.bucket_start < end,
    )
    groups: dict[int, dict[str, Any]] = {}
    stream = await db.stream(statement, execution_options={"yield_per": STREAM_PAGE_SIZE})
    try:
        async for row in stream:
            at = (
                row.bucket_start.replace(tzinfo=UTC)
                if row.bucket_start.tzinfo is None
                else row.bucket_start.astimezone(UTC)
            )
            data = row.payload
            values = data.get("values", {})
            # Only complete, normally placed minutes may use their stored statistics.
            whole = start <= at and at + timedelta(minutes=1) <= end
            minute_second = at.timestamp()
            if whole and all(minute_second <= int(k) < minute_second + 60 for k in values):
                _whole_minute(groups, int(minute_second // seconds), data)
                continue
            sources, methods = data.get("sources", {}), data.get("methods", {})
            for stamp, value in values.items():
                timestamp, _ = _slot_time(stamp, data.get("metadata", {}))
                if not start <= timestamp < end:
                    continue
                group = groups.setdefault(int(timestamp.timestamp() // seconds), _group())
                _provenance(group, stamp, value, sources, methods)
                _stats(
                    group, int(value is not None), int(value is None), value or 0.0, value, value
                )
                _last(group, stamp, value, data, sources, methods)
    finally:
        await stream.close()
    return groups
