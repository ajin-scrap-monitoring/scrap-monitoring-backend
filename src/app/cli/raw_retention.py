"""Preview bounded expiration by default; writes require --apply and explicit configuration."""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import text

from src.app.core.config import get_settings
from src.app.db.migration import require_current_schema
from src.app.db.session import close_db, get_engine, get_sessionmaker
from src.app.services.raw_retention import RetentionCursor, retention_batch


async def run(*, apply: bool, max_batches: int) -> dict[str, object]:
    if not 1 <= max_batches <= 1000:
        raise ValueError("max-batches must be 1..1000")
    settings = get_settings()
    cursor = RetentionCursor()
    now = datetime.now(UTC)
    results: list[dict[str, object]] = []
    try:
        async with get_engine().connect() as connection:
            await connection.run_sync(require_current_schema)
        for _ in range(max_batches):
            async with get_sessionmaker()() as db:
                if not apply and db.get_bind().dialect.name == "postgresql":
                    await db.execute(text("SET TRANSACTION READ ONLY"))
                result, next_cursor = await retention_batch(
                    db, cursor, apply=apply, settings=settings, now=now
                )
                if apply:
                    await db.commit()
                else:
                    await db.rollback()
                cursor = next_cursor
                results.append(asdict(result))
                if result.exhausted:
                    break
        return {
            "scope": "bounded_batches_not_whole_database",
            "batches_processed": len(results),
            "results": results,
        }
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-batches", type=int, default=1)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(apply=args.apply, max_batches=args.max_batches))
    except Exception as exc:
        # Never include raw SQL, arguments, credentials or wire bodies in CLI errors.
        print(json.dumps({"error": "RAW_RETENTION_FAILED", "code": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report))


if __name__ == "__main__":
    main()
