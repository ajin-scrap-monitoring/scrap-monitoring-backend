"""Explicit, bounded maintenance of the existing history receipt statistics."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.db.migration import require_current_schema
from src.app.models.edge_v2 import EdgeV2Message
from src.app.schemas.monitoring_v2 import MonitoringSource

COLUMNS = ("site_id", "edge_id", "message_type", "compact", "generated_at")


class StatisticsError(Exception):
    """Only a stable code, never an SQL error or connection string, reaches the CLI."""


def assess(
    latest: datetime | None,
    sampled: datetime | None,
    max_lag_seconds: int,
    now: datetime,
    *,
    statistics_present: bool = False,
) -> dict[str, Any]:
    if latest is None:
        return {"reason": "NO_DATA", "refreshNeeded": False, "lagSeconds": None}
    if latest > now + timedelta(seconds=5):
        return {"reason": "FUTURE_DATA", "refreshNeeded": False, "lagSeconds": None}
    if sampled is None and statistics_present:
        return {
            "reason": "STATISTICS_RANGE_UNAVAILABLE",
            "refreshNeeded": False,
            "lagSeconds": None,
        }
    if sampled is not None and sampled > now + timedelta(seconds=5):
        return {"reason": "FUTURE_STATISTICS", "refreshNeeded": False, "lagSeconds": None}
    lag = max(0.0, (latest - sampled).total_seconds()) if sampled else None
    needed = lag is None or lag > max_lag_seconds
    return {
        "reason": "STATISTICS_BEHIND" if needed else "WITHIN_LAG_THRESHOLD",
        "refreshNeeded": needed,
        "lagSeconds": lag,
    }


async def inspect_statistics(
    engine: AsyncEngine,
    sources: list[MonitoringSource],
    max_lag_seconds: int,
    statement_seconds: int,
    lock_seconds: int,
) -> dict[str, Any]:
    """Use catalog metadata and bounded source/time index lookups, never a receipt scan."""
    async with engine.begin() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(
            text(
                "SELECT set_config('statement_timeout', :s, true), "
                "set_config('lock_timeout', :l, true)"
            ),
            {"s": str(statement_seconds * 1000), "l": str(lock_seconds * 1000)},
        )
        version = int(await conn.scalar(text("SHOW server_version_num")) or 0)
        if version // 10000 != 17:
            raise StatisticsError("POSTGRESQL_17_REQUIRED")
        await conn.run_sync(require_current_schema)
        relation = (
            (
                await conn.execute(
                    text(
                        "SELECT n.nspname AS schema_name, c.relname AS table_name, "
                        "has_table_privilege(c.oid, 'MAINTAIN') OR "
                        "pg_has_role(d.datdba, 'USAGE') AS can_analyze "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "JOIN pg_database d ON d.datname=current_database() "
                        "WHERE c.oid=to_regclass('edge_v2_messages') AND c.relkind='r'"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if relation is None:
            raise StatisticsError("HISTORY_TABLE_NOT_FOUND")
        schema = relation["schema_name"]
        stats = (
            (
                await conn.execute(
                    text(
                        "SELECT to_json(histogram_bounds)::text AS histogram, "
                        "to_json(most_common_vals)::text AS common "
                        "FROM pg_stats WHERE schemaname=:schema AND tablename='edge_v2_messages' "
                        "AND attname='generated_at' AND NOT inherited"
                    ),
                    {"schema": schema},
                )
            )
            .mappings()
            .one_or_none()
        )
        # Common values can cover an entire single-valued population without a histogram.
        samples = []
        if stats:
            for field in ("histogram", "common"):
                for value in json.loads(stats[field] or "[]"):
                    stamp = datetime.fromisoformat(value)
                    if stamp.tzinfo is None:
                        raise StatisticsError("STATISTICS_TIMEZONE_MISSING")
                    samples.append(stamp.astimezone(UTC))
        sampled = max(samples, default=None)
        latest_values = []
        for source in sources:
            latest = await conn.scalar(
                select(EdgeV2Message.generated_at)
                .where(
                    EdgeV2Message.site_id == source.site_id,
                    EdgeV2Message.edge_id == source.edge_id,
                    EdgeV2Message.message_type == "edge_assessment",
                )
                .order_by(EdgeV2Message.generated_at.desc())
                .limit(1)
            )
            if latest is not None:
                latest_values.append(latest.astimezone(UTC))
        newest = max(latest_values, default=None)
        now = datetime.now(UTC)
        return {
            "schema": schema,
            "table": "edge_v2_messages",
            "columns": list(COLUMNS),
            "canAnalyze": bool(relation["can_analyze"]),
            "checkedAt": now.isoformat(),
            "sampleLatestAt": sampled.isoformat() if sampled else None,
            "dataLatestAt": newest.isoformat() if newest else None,
            "sourceCount": len(sources),
            "maxLagSeconds": max_lag_seconds,
            **assess(newest, sampled, max_lag_seconds, now, statistics_present=stats is not None),
        }


async def maintain_statistics(
    engine: AsyncEngine,
    sources: list[MonitoringSource],
    operation: str,
    max_lag_seconds: int = 86400,
    statement_seconds: int = 60,
    lock_seconds: int = 1,
) -> dict[str, Any]:
    """Refresh only explicitly requested, lagging statistics; never change data or grants."""
    if engine.dialect.name != "postgresql":
        raise StatisticsError("POSTGRESQL_17_REQUIRED")
    if operation not in ("check", "refresh") or not (
        1 <= max_lag_seconds <= 604800 and 1 <= statement_seconds <= 60 and 1 <= lock_seconds <= 5
    ):
        raise StatisticsError("INVALID_OPTIONS")
    if not sources:
        raise StatisticsError("MONITORING_SOURCES_REQUIRED")
    try:
        async with asyncio.timeout(statement_seconds + 10):
            before = await inspect_statistics(
                engine, sources, max_lag_seconds, statement_seconds, lock_seconds
            )
            result = {"operation": operation, "action": "CHECKED", "before": before}
            if operation == "check":
                return result
            if not before["refreshNeeded"]:
                return {**result, "action": "SKIPPED"}
            if not before["canAnalyze"]:
                raise StatisticsError("STATISTICS_PERMISSION_REQUIRED")
            # Quote the catalog-resolved schema, not a caller-provided SQL identifier.
            quote = engine.dialect.identifier_preparer.quote
            target = quote(before["schema"]) + "." + quote("edge_v2_messages")
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "SELECT set_config('statement_timeout', :s, true), "
                        "set_config('lock_timeout', :l, true)"
                    ),
                    {"s": str(statement_seconds * 1000), "l": str(lock_seconds * 1000)},
                )
                await conn.execute(
                    text(f"ANALYZE (BUFFER_USAGE_LIMIT '2MB') {target} ({', '.join(COLUMNS)})")
                )
            after = await inspect_statistics(
                engine, sources, max_lag_seconds, statement_seconds, lock_seconds
            )
            # Sampling or concurrent arrivals may still leave lag. Never report it as fixed.
            return {**result, "action": "ANALYZED", "after": after}
    except TimeoutError:
        raise StatisticsError("STATISTICS_OPERATION_TIMEOUT") from None
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None)
        safe = {
            "42501": "STATISTICS_PERMISSION_REQUIRED",
            "55P03": "STATISTICS_LOCK_TIMEOUT",
            "57014": "STATISTICS_STATEMENT_TIMEOUT",
        }
        raise StatisticsError(safe.get(str(code), "STATISTICS_DATABASE_ERROR")) from None
