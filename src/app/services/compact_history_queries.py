"""Reduce complete minute rows in SQL before loading compact history into Python."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Float, String, case, cast, extract, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.schemas.monitoring_v2 import MonitoringSource


def _text(db: AsyncSession, payload: Any, section: str, key: Any) -> Any:
    if db.get_bind().dialect.name == "postgresql":
        return func.json_extract_path_text(payload, section, key)
    return func.json_extract(payload, "$." + section + '."' + key + '"')


def _object(db: AsyncSession, payload: Any, section: str, key: Any = None) -> Any:
    if db.get_bind().dialect.name == "postgresql":
        return (
            func.json_extract_path(payload, section, key)
            if key is not None
            else func.json_extract_path(payload, section)
        )
    path = "$." + section + '."' + key + '"' if key is not None else "$." + section
    return func.json_extract(payload, path)


def compact_aggregate_query(
    db: AsyncSession, source: MonitoringSource, start: datetime, end: datetime, seconds: int
) -> Any:
    """Return one small summary per bucket, including only the last slot's metadata.

    Callers handle at most two partially selected minutes separately so SQL never
    rounds away microsecond boundaries. Every supported interval is a whole minute.
    The composite minute primary key supplies the source/time range and final lookup.
    """
    minute = EdgeV2Minute
    postgres = db.get_bind().dialect.name == "postgresql"
    # Parse each scalar map once per minute, not once per slot/aggregate expression.
    # Exclude the much larger per-slot metadata map from the materialized working set.
    selected = (
        select(
            minute.bucket_start,
            *[
                (cast(minute.payload[name], JSONB) if postgres else minute.payload[name]).label(
                    name
                )
                for name in ("values", "sources", "methods")
            ],
        )
        .where(
            minute.site_id == source.site_id,
            minute.edge_id == source.edge_id,
            minute.bucket_start >= start,
            minute.bucket_start < end,
        )
        .cte("compact_minutes")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    function = func.jsonb_each_text if postgres else func.json_each
    slots = function(selected.c["values"]).table_valued("key", "value", joins_implicitly=True)
    value = cast(slots.c.value, Float)
    method = (
        func.jsonb_extract_path_text(selected.c.methods, slots.c.key)
        if postgres
        else func.json_extract(selected.c.methods, '$."' + slots.c.key + '"')
    )
    slot_source = (
        func.jsonb_extract_path_text(selected.c.sources, slots.c.key)
        if postgres
        else func.json_extract(selected.c.sources, '$."' + slots.c.key + '"')
    )
    input_source = func.coalesce(slot_source, "EDGE_STATE")
    valid = value.is_not(None)
    bucket = func.floor(extract("epoch", selected.c.bucket_start) / seconds)
    # Keep Python's case-sensitive prefix rule on SQLite as well as PostgreSQL.
    simulated = (
        func.substr(method, 1, len("SIMULATION_HEIGHT_SECTION_V1:"))
        == "SIMULATION_HEIGHT_SECTION_V1:"
    ) | (
        func.substr(method, 1, len("SIMULATION_SENSOR_ANGLE_V1:")) == "SIMULATION_SENSOR_ANGLE_V1:"
    )
    grouped = (
        select(
            bucket.label("bucket"),
            func.sum(value).label("total"),
            func.count(value).label("count"),
            (func.count() - func.count(value)).label("invalid"),
            func.min(value).label("minimum"),
            func.max(value).label("maximum"),
            func.count(func.distinct(case((valid, input_source)))).label("source_count"),
            func.min(case((valid, input_source))).label("source"),
            func.count(func.distinct(case((valid, method)))).label("method_count"),
            func.min(case((valid, method))).label("method"),
            func.max(case((simulated, 1), else_=0)).label("simulated"),
            func.max(cast(slots.c.key, BigInteger)).label("stamp"),
            func.max(selected.c.bucket_start).label("last_minute"),
        )
        .select_from(selected)
        .join(slots, true())
        .where(slots.c.key.is_not(None))
        .group_by(bucket)
        .cte("compact_buckets")
    )
    key = cast(grouped.c.stamp, String)
    return (
        select(
            grouped,
            cast(_text(db, minute.payload, "values", key), Float).label("value"),
            _text(db, minute.payload, "sources", key).label("last_source"),
            _text(db, minute.payload, "methods", key).label("last_method"),
            _object(db, minute.payload, "metadata", key).label("metadata"),
            _object(db, minute.payload, "last").label("fallback"),
        )
        .join(
            minute,
            (minute.site_id == source.site_id)
            & (minute.edge_id == source.edge_id)
            & (minute.bucket_start == grouped.c.last_minute),
        )
        .order_by(grouped.c.bucket)
    )
