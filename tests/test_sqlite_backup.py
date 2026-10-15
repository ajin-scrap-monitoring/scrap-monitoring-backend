"""Recovery boundaries for WAL snapshots and source/destination preservation."""

import hashlib
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing

import pytest

from src.app.db.sqlite_backup import BackupError, backup_sqlite


def test_wal_snapshot_restores_latest_commits_without_source_sidecars(tmp_path):
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    with closing(sqlite3.connect(source)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, value TEXT)")
        live.execute("INSERT INTO samples VALUES (1, 'synthetic-한글')")
        live.commit()
        wal = source.with_name(source.name + "-wal")
        assert wal.stat().st_size > 0
        original = hashlib.sha256(source.read_bytes() + wal.read_bytes()).digest()
        backup_sqlite(source, output)
        assert hashlib.sha256(source.read_bytes() + wal.read_bytes()).digest() == original
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        # The backup must work as a single file in a different directory, with no WAL/SHM.
        recovery = tmp_path / "recovered"
        recovery.mkdir()
        restored = recovery / "database.db"
        restored.write_bytes(output.read_bytes())
        with closing(sqlite3.connect(restored)) as db:
            assert db.execute("SELECT * FROM samples").fetchall() == [(1, "synthetic-한글")]
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert not list(tmp_path.glob(".sqlite-backup-*"))
        assert not output.with_name(output.name + "-wal").exists()


def test_existing_destination_and_source_are_never_overwritten(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE example (id INTEGER)")
    output = tmp_path / "preserved.db"
    output.write_bytes(b"synthetic-existing-backup")
    for destination in (source, output):
        original = destination.read_bytes()
        with pytest.raises(BackupError, match="overwriting is forbidden"):
            backup_sqlite(source, destination)
        assert destination.read_bytes() == original


@pytest.mark.parametrize("kind", ["missing", "corrupt", "orphan"])
def test_invalid_sources_publish_no_backup_or_partial_file(tmp_path, kind):
    source = tmp_path / "source.db"
    if kind == "corrupt":
        source.write_bytes(b"synthetic-not-a-database")
    elif kind == "orphan":
        with closing(sqlite3.connect(source)) as db:
            db.executescript(
                "CREATE TABLE parents (id INTEGER PRIMARY KEY);"
                "CREATE TABLE children (parent_id INTEGER REFERENCES parents(id));"
                "INSERT INTO children VALUES (99);"
            )
    output = tmp_path / "backup.db"
    with pytest.raises(BackupError):
        backup_sqlite(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".sqlite-backup-*"))


def test_locked_database_backup_has_bounded_wait_and_no_published_file(tmp_path):
    source = tmp_path / "source.db"
    output = tmp_path / "backup.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE example (id INTEGER)")
        db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(BackupError, match="timed out"):
            backup_sqlite(source, output, timeout_seconds=0.15)
        db.rollback()
    assert not output.exists()


def test_cli_preserves_unregistered_legacy_db_and_reports_failure_safely(tmp_path):
    source = tmp_path / "legacy.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE legacy (value TEXT)")
        db.execute("INSERT INTO legacy VALUES ('synthetic-private-row')")
        db.commit()
    output = tmp_path / "backup.db"
    command = [
        sys.executable,
        "-m",
        "src.app.cli.backup",
        "--source",
        str(source),
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "integrity and foreign keys verified" in result.stdout
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert repeated.returncode != 0
    assert "synthetic-private-row" not in result.stdout + result.stderr + repeated.stderr
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT * FROM legacy").fetchall() == [("synthetic-private-row",)]
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='alembic_version'"
        ).fetchall()
