"""Check history statistics or explicitly refresh the fixed history columns on PostgreSQL 17."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import create_async_engine

from src.app.core.config import get_settings
from src.app.services.history_statistics import StatisticsError, maintain_statistics


async def run(operation: str, max_lag: int, statement: int, lock: int) -> dict[str, object]:
    settings = get_settings()
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg:"):
        raise StatisticsError("POSTGRESQL_17_REQUIRED")
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        connect_args={"timeout": 10},
    )
    try:
        return await maintain_statistics(
            engine, settings.EDGE_V2_MONITORING_SOURCES, operation, max_lag, statement, lock
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "refresh"))
    parser.add_argument("--max-lag-seconds", type=int, default=86400)
    parser.add_argument("--statement-timeout-seconds", type=int, default=60)
    parser.add_argument("--lock-timeout-seconds", type=int, default=1)
    args = parser.parse_args()
    if not (
        1 <= args.max_lag_seconds <= 604800
        and 1 <= args.statement_timeout_seconds <= 60
        and 1 <= args.lock_timeout_seconds <= 5
    ):
        parser.error("max-lag must be 1..604800, statement-timeout 1..60, lock-timeout 1..5")
    started = datetime.now(UTC).isoformat()
    clock = time.monotonic()
    failed = False
    try:
        result = asyncio.run(
            run(
                args.operation,
                args.max_lag_seconds,
                args.statement_timeout_seconds,
                args.lock_timeout_seconds,
            )
        )
    except Exception as exc:
        failed = True
        result = {
            "operation": args.operation,
            "action": "FAILED",
            "errorCode": str(exc)
            if isinstance(exc, StatisticsError)
            else "STATISTICS_OPERATION_FAILED",
        }
    result.update(
        startedAt=started,
        completedAt=datetime.now(UTC).isoformat(),
        elapsedSeconds=round(time.monotonic() - clock, 3),
    )
    print(json.dumps(result))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
