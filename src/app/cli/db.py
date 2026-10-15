"""Explicit database migration/status commands; credentials come only from settings."""

import argparse
import asyncio
import sys

from src.app.db.migration import current_revisions, require_current_schema, upgrade_schema
from src.app.db.schema_guard import SchemaStateError
from src.app.db.session import close_db, get_engine


async def run(operation: str, adopt_legacy: bool = False) -> None:
    try:
        async with get_engine().begin() as connection:
            if operation == "upgrade":
                await connection.run_sync(upgrade_schema, adopt_legacy)
                await connection.run_sync(require_current_schema)
                print("Database migration completed")
            elif operation == "check":
                await connection.run_sync(require_current_schema)
                print("Database schema is current")
            else:
                revisions = await connection.run_sync(current_revisions)
                print("Current revision: " + (", ".join(revisions) if revisions else "unversioned"))
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("upgrade", "check", "current"))
    parser.add_argument(
        "--adopt-legacy",
        action="store_true",
        help="Register a backed-up, compatible unversioned database",
    )
    args = parser.parse_args()
    if args.adopt_legacy and args.operation != "upgrade":
        parser.error("--adopt-legacy is only valid with upgrade")
    try:
        asyncio.run(run(args.operation, args.adopt_legacy))
    except SchemaStateError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            "Database operation failed; check configuration and migration status before retrying",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
