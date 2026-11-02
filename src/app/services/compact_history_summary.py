"""Small derived minute summaries, with bounded original-evidence fallback reads."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.compact_history_stream import (
    STREAM_PAGE_SIZE,
    _distinct,
    _group,
    _stats,
    _whole_minute,
    accumulate_minute,
)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def build_minute_summary(
    at: datetime, generated_at: datetime, payload: dict[str, Any]
) -> dict[str, Any]:
    """Summarize only normal complete minutes; preserve every original evidence byte."""
    at = utc(at)
    result: dict[str, Any] = {
        "version": 1,
        "bucketStartAt": at.isoformat(),
        "generatedAt": utc(generated_at).isoformat(),
        "usable": False,
    }
    values = payload.get("values", {})
    if not values or len(values) > 60:
        return result
    second = at.timestamp()
    if not all(second <= int(key) < second + 60 for key in values):
        return result
    groups: dict[int, dict[str, Any]] = {}
    _whole_minute(groups, 0, payload)
    group = groups[0]
    result.update(
        usable=True,
        group={
            **group,
            "sources": sorted(group["sources"]),
            "methods": sorted(group["methods"]),
            "last_time": group["last_time"].isoformat(),
        },
    )
    return result


def restore_summary(
    summary: dict[str, Any] | None, at: datetime, generated_at: datetime
) -> dict[str, Any] | None:
    """Unknown formats or mismatched row generations must read original evidence."""
    if (
        not isinstance(summary, dict)
        or type(summary.get("version")) is not int
        or summary.get("version") != 1
        or summary.get("usable") is not True
        or summary.get("bucketStartAt") != at.isoformat()
        or summary.get("generatedAt") != utc(generated_at).isoformat()
    ):
        return None
    try:
        group = dict(summary["group"])
        required = {
            "count",
            "invalid",
            "total",
            "minimum",
            "maximum",
            "sources",
            "methods",
            "simulated",
            "mixed",
            "last_time",
            "value",
            "last",
            "last_source",
            "last_method",
        }
        if not required <= group.keys():
            return None
        if any(type(group[k]) is not int or not 0 <= group[k] <= 60 for k in ("count", "invalid")):
            return None
        if not 0 < group["count"] + group["invalid"] <= 60 or not math.isfinite(group["total"]):
            return None
        group["last_time"] = datetime.fromisoformat(group["last_time"])
        if not at <= group["last_time"] < at + timedelta(minutes=1):
            return None
        for field in ("sources", "methods"):
            if (
                not isinstance(group[field], list)
                or len(group[field]) > 2
                or not all(isinstance(value, str) for value in group[field])
            ):
                return None
            group[field] = set(group[field])
        for field in ("minimum", "maximum", "value"):
            value = group[field]
            if value is not None and (
                not isinstance(value, (int, float)) or not math.isfinite(value)
            ):
                return None
        if group["count"] and (group["minimum"] is None or group["maximum"] is None):
            return None
        if any(type(group[field]) is not bool for field in ("simulated", "mixed")):
            return None
        if (
            not {"generatedAt", "observedAt", "assessmentId", "dataState", "reasonCodes"}
            <= group["last"].keys()
        ):
            return None
        return group
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def merge_summary(target: dict[str, Any], part: dict[str, Any]) -> None:
    _stats(target, part["count"], part["invalid"], part["total"], part["minimum"], part["maximum"])
    for field in ("sources", "methods"):
        for value in part[field]:
            _distinct(target, field, value)
    target["simulated"] |= part["simulated"]
    target["mixed"] |= part["mixed"]
    if "last_time" not in target or part["last_time"] >= target["last_time"]:
        for field in ("last_time", "value", "last", "last_source", "last_method"):
            target[field] = part[field]


async def summary_stream_groups(
    db: AsyncSession, source: MonitoringSource, start: datetime, end: datetime, seconds: int
) -> dict[int, dict[str, Any]]:
    """Read small rows first, fetching at most one page of originals when necessary."""
    minute = EdgeV2Minute
    statement = select(minute.bucket_start, minute.generated_at, minute.history_summary).where(
        minute.site_id == source.site_id,
        minute.edge_id == source.edge_id,
        minute.bucket_start >= start.replace(second=0, microsecond=0),
        minute.bucket_start < end,
    )
    groups: dict[int, dict[str, Any]] = {}
    stream = await db.stream(statement, execution_options={"yield_per": STREAM_PAGE_SIZE})
    try:
        async for page in stream.partitions(STREAM_PAGE_SIZE):
            originals = []
            parts = {}
            for row in page:
                at = utc(row.bucket_start)
                whole = start <= at and at + timedelta(minutes=1) <= end
                part = restore_summary(row.history_summary, at, row.generated_at) if whole else None
                if part is None:
                    originals.append(row.bucket_start)
                else:
                    parts[row.bucket_start] = part
            original_data = {}
            if originals:
                # Unfilled legacy rows and the at-most-two partial boundary minutes
                # keep the original slot/metadata policy. Never load the whole range.
                rows = await db.execute(
                    select(minute.bucket_start, minute.payload).where(
                        minute.site_id == source.site_id,
                        minute.edge_id == source.edge_id,
                        minute.bucket_start.in_(originals),
                    )
                )
                for row in rows:
                    original_data[row.bucket_start] = row.payload
            # Preserve this page's read order even when cached and original rows mix.
            for row in page:
                at = utc(row.bucket_start)
                part = parts.get(row.bucket_start)
                if part is not None:
                    merge_summary(groups.setdefault(int(at.timestamp() // seconds), _group()), part)
                elif row.bucket_start in original_data:
                    accumulate_minute(
                        groups, at, original_data[row.bucket_start], start, end, seconds
                    )
    finally:
        await stream.close()
    return groups
