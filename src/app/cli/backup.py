"""Create a verified SQLite backup, including committed WAL pages, without changing the source."""

import argparse
import sys
from pathlib import Path

from src.app.db.sqlite_backup import BackupError, backup_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        backup_sqlite(args.source, args.output, timeout_seconds=args.timeout_seconds)
    except BackupError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    print("SQLite backup completed; integrity and foreign keys verified")


if __name__ == "__main__":
    main()
