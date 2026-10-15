"""Offline SQLite-to-PostgreSQL transfer; destination credentials come from settings."""

import argparse
import asyncio
import sys
from pathlib import Path

from src.app.core.config import get_settings
from src.app.db.schema_guard import SchemaStateError
from src.app.db.transfer import TransferError, transfer_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "apply", "verify"))
    parser.add_argument("--source", required=True, type=Path, help="Stopped, backed-up SQLite DB")
    parser.add_argument(
        "--batch-size", type=int, default=50, choices=range(1, 501), metavar="1..500"
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(
            transfer_database(
                args.source,
                get_settings().DATABASE_URL,
                mode=args.operation,
                batch_size=args.batch_size,
            )
        )
    except (TransferError, SchemaStateError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Transfer failed; check configuration and DB state", file=sys.stderr)
        raise SystemExit(1) from None
    for name, summary in result.items():
        print(f"{name}: {summary.rows} rows verified")
    status = {
        "check": "Trial completed; all changes rolled back",
        "apply": "Transfer committed after full comparison",
        "verify": "Stored data matches source",
    }
    print(status[args.operation])


if __name__ == "__main__":
    main()
