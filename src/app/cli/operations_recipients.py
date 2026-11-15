"""Refresh an operating recipient cache without starting the web app or sending mail."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.app.operations.recipients import RecipientCacheError, refresh_recipients


async def refresh(path: Path, maximum_age: int) -> dict[str, object]:
    url = os.environ.get("OPS_RECIPIENT_DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg://"):
        raise RecipientCacheError("OPERATIONS_RECIPIENT_DATABASE_REQUIRED")
    engine = create_async_engine(url, hide_parameters=True)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        snapshot, cached = await refresh_recipients(
            factory, path, datetime.now(UTC), max_age_seconds=maximum_age
        )
        return {
            "source": "last_good_cache" if cached else "database",
            "generated_at": snapshot.generated_at.isoformat(),
            "recipient_count": len(snapshot.recipients),
            "notificationSent": False,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=int, default=86400)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(refresh(args.cache, args.max_age_seconds))))
    except Exception:
        # Engine/configuration errors may contain a private DSN. Never print them.
        print(
            json.dumps({"error": "OPERATIONS_RECIPIENT_REFRESH_FAILED", "notificationSent": False})
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
