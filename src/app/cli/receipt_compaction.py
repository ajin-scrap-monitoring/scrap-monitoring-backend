"""Preview receipt metadata reduction; --apply explicitly commits bounded batches."""

import argparse
import asyncio
import json
from dataclasses import asdict

from sqlalchemy import text

from src.app.db.migration import require_current_schema
from src.app.db.session import close_db, get_engine, get_sessionmaker
from src.app.services.receipt_compaction import compact_receipts


async def run(
    *, apply: bool = False, after_id: int = 0, batch_size: int = 100, max_batches: int = 1
) -> dict[str, object]:
    if not 1 <= max_batches <= 1000:
        raise ValueError("max-batches must be 1..1000")
    results: list[dict[str, object]] = []
    try:
        async with get_engine().connect() as conn:
            await conn.run_sync(require_current_schema)
        for _ in range(max_batches):
            async with get_sessionmaker()() as db:
                if db.get_bind().dialect.name == "postgresql":
                    if not apply:
                        await db.execute(text("SET TRANSACTION READ ONLY"))
                    await db.execute(text("SET LOCAL statement_timeout='5s'"))
                    await db.execute(text("SET LOCAL lock_timeout='1s'"))
                result = await compact_receipts(
                    db, after_id=after_id, batch_size=batch_size, apply=apply
                )
                if apply:
                    await db.commit()
                else:
                    await db.rollback()
                results.append(asdict(result))
                after_id = result.next_after_id
                if result.exhausted:
                    break
            # Yield between committed batches; do not monopolize the receiver's DB.
            await asyncio.sleep(0.1)
        return {"scope": "bounded_receipt_batches_not_whole_database", "results": results}
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=1)
    args = parser.parse_args()
    try:
        report = asyncio.run(
            run(
                apply=args.apply,
                after_id=args.after_id,
                batch_size=args.batch_size,
                max_batches=args.max_batches,
            )
        )
    except Exception as exc:
        print(json.dumps({"error": "RECEIPT_COMPACTION_FAILED", "code": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report))


if __name__ == "__main__":
    main()
