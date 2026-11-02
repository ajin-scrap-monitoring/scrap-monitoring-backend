"""Inspect or fill optional history summaries in a migrated database, in bounded batches."""

import argparse
import asyncio
import json
import math
import sys

from src.app.db.migration import require_current_schema
from src.app.db.session import close_db, get_engine, get_sessionmaker
from src.app.services.history_summary_backfill import MinuteKey, backfill_batch, missing_summaries


async def run(operation: str, batch_size: int, max_batches: int, pause_seconds: float) -> None:
    try:
        async with get_engine().connect() as connection:
            await connection.run_sync(require_current_schema)
        updated, batches = 0, 0
        cursor: MinuteKey | None = None
        async with get_sessionmaker()() as db:
            if operation == "backfill":
                for _ in range(max_batches):
                    count, cursor = await backfill_batch(db, batch_size, cursor)
                    if not count:
                        break
                    updated += count
                    batches += 1
                    print(json.dumps({"batches": batches, "updated": updated}), flush=True)
                    if pause_seconds:
                        await asyncio.sleep(pause_seconds)
            remaining = await missing_summaries(db)
            print(
                json.dumps({"updated": updated, "remaining": remaining, "complete": remaining == 0})
            )
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "backfill"))
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--pause-seconds", type=float, default=0.1)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 512 or not 1 <= args.max_batches <= 10000:
        parser.error("batch-size must be 1..512 and max-batches must be 1..10000")
    if not math.isfinite(args.pause_seconds) or not 0 <= args.pause_seconds <= 5:
        parser.error("pause-seconds must be a finite value within 0..5")
    try:
        asyncio.run(run(args.operation, args.batch_size, args.max_batches, args.pause_seconds))
    except Exception:
        print(
            "History summary operation failed; original observations are retained", file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
