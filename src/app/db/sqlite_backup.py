"""Standalone SQLite snapshot API for administrator CLI use, never a request handler."""

import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


class BackupError(RuntimeError):
    """Public error without database contents, SQL parameters or credentials."""


def backup_sqlite(source: Path, output: Path, *, timeout_seconds: float = 30) -> None:
    """Include committed WAL data; publish a verified standalone file without overwriting."""
    partial: Path | None = None
    try:
        if not 0 < timeout_seconds <= 3600:
            raise BackupError("Backup timeout must be greater than zero and at most 3600 seconds")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise BackupError("Source must be an existing SQLite file")
        output = output.parent.resolve(strict=True) / output.name
        if os.path.lexists(output):
            raise BackupError("Backup destination already exists; overwriting is forbidden")
        descriptor, temporary = tempfile.mkstemp(prefix=".sqlite-backup-", dir=output.parent)
        os.close(descriptor)
        partial = Path(temporary)
        deadline = time.monotonic() + timeout_seconds

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise BackupError("SQLite backup timed out; no success confirmed")

        def interrupt() -> int:
            return int(time.monotonic() >= deadline)

        # Do not use immutable=1: it can hide committed pages still in the WAL.
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as reader,
            closing(sqlite3.connect(partial)) as writer,
        ):
            reader.execute("PRAGMA query_only=ON")
            reader.backup(writer, pages=128, progress=progress, sleep=0.05)
            writer.set_progress_handler(interrupt, 1000)
            writer.execute("PRAGMA journal_mode=DELETE")
            if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("SQLite backup integrity check failed")
            if writer.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("SQLite backup foreign-key check failed")
        with partial.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        # link() fails if any destination entry exists, including a racing writer/symlink.
        # Both names are in the same directory/filesystem; no incomplete DB is published.
        os.link(partial, output)
        descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BackupError:
        raise
    except Exception:
        raise BackupError(
            "SQLite backup failed; no success confirmed. Check files before retrying"
        ) from None
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
