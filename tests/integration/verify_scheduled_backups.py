"""Validate the actual backup worker, retention and SQLite-WAL-to-PG recovery locally."""

import argparse
import asyncio
import hashlib
import os
import secrets
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from src.app.db.sqlite_backup import backup_sqlite
from tests.integration.verify_db_migrations import scram_verifier
from tests.integration.verify_postgresql_deployment import invoke
from tests.transfer_fixtures import seed_transfer_source

ROOT = Path(__file__).resolve().parents[2]


def verify(image, directory):
    source = directory / "live.db"
    asyncio.run(seed_transfer_source(source))
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO pit_configs SELECT 'pit-wal', name, total_depth_cm, "
            "warning_threshold_percent, critical_threshold_percent, updated_at, created_at "
            "FROM pit_configs LIMIT 1"
        )
        writer.commit()
        wal = source.with_name(source.name + "-wal")
        assert wal.stat().st_size > 0
        original = hashlib.sha256(source.read_bytes() + wal.read_bytes()).digest()
        snapshot = directory / "snapshot.db"
        backup_sqlite(source, snapshot)
        assert hashlib.sha256(source.read_bytes() + wal.read_bytes()).digest() == original
    finally:
        writer.close()
    with sqlite3.connect(snapshot) as restored:
        assert restored.execute(
            "SELECT count(*) FROM pit_configs WHERE id='pit-wal'"
        ).fetchone() == (1,)
    # A public synthetic transfer copy is readable by the non-root management container.
    # The actual backup remains mode 0600; no real operating data is used here.
    transfer = directory / "source.db"
    transfer.write_bytes(snapshot.read_bytes())
    transfer.chmod(0o444)
    directory.chmod(0o755)
    print("Standalone SQLite snapshot includes committed WAL; source bytes unchanged", flush=True)

    project = "scheduled-backup-check-" + uuid4().hex[:10]
    volume = project + "-store"
    settings = directory / "backend.env"
    settings.write_text("EDGE_API_KEY=synthetic-backup-edge-key\n")
    passwords = {role: secrets.token_urlsafe(32) for role in ("ADMIN", "OWNER", "APP", "BACKUP")}
    environment = dict(
        os.environ,
        BACKEND_IMAGE=image,
        BACKEND_ENV_FILE=str(settings),
        POSTGRES_IMAGE="postgres:17-alpine",
        POSTGRES_BACKUP_VOLUME=volume,
        DB_BACKUP_PASSWORD=passwords["BACKUP"],
        DB_OWNER_PASSWORD_URLENCODED=quote(passwords["OWNER"], safe=""),
        DB_APP_PASSWORD_URLENCODED=quote(passwords["APP"], safe=""),
        BACKUP_RETAIN_COUNT="2",
        BACKUP_INTERVAL_SECONDS="1",
        BACKUP_TIMEOUT_SECONDS="30",
        **{f"DB_{role}_SCRAM": scram_verifier(value) for role, value in passwords.items()},
    )
    base = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        project,
        "-f",
        "docker-compose.postgresql.yml",
        "-f",
        "docker-compose.backup.yml",
    ]

    def compose(*args, env=None, success=True):
        return invoke([*base, *args], env or environment, success=success)

    def bundles():
        return compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "bash",
            "backup",
            "-c",
            "find /backups -mindepth 1 -maxdepth 1 -type d -name 'backup-*' | sort",
        ).stdout.splitlines()

    holder = project + "-lock"
    invoke(["docker", "volume", "create", volume], environment)
    try:
        compose("config", "--quiet")
        compose("up", "-d", "--wait", "--wait-timeout", "90", "postgres")
        unprepared = compose("run", "--rm", "backup", "once", success=False)
        assert unprepared.returncode != 0 and "stage=source_check" in unprepared.stderr
        assert bundles() == []
        compose("run", "--rm", "manage", "-m", "src.app.cli.db", "upgrade")
        mount = str(directory) + ":/migration:ro"
        compose(
            "run",
            "--rm",
            "--volume",
            mount,
            "manage",
            "-m",
            "src.app.cli.transfer",
            "apply",
            "--source",
            "/migration/source.db",
        )
        compose(
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "psql",
            "--no-password",
            "--dbname=scrap_monitoring",
            "--file=/opt/scrap-postgresql/runtime-grants.sql",
        )
        for _ in range(3):
            compose("run", "--rm", "backup", "once")
        before = bundles()
        assert len(before) == 2 and "0000000002" in before[0] and "0000000003" in before[1]
        compose("run", "--rm", "backup", "health")
        print(
            "Real read-only-role dumps, checksum publication and successful-only retention passed",
            flush=True,
        )

        compose(
            "run",
            "--rm",
            "-d",
            "--name",
            holder,
            "--user",
            "postgres",
            "--entrypoint",
            "bash",
            "backup",
            "-c",
            "exec 9>/backups/.lock; flock 9; touch /backups/hold-ready; sleep 60",
        )
        for _ in range(20):
            ready = compose(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "test",
                "backup",
                "-f",
                "/backups/hold-ready",
                success=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Lock holder readiness timed out")
        blocked = compose("run", "--rm", "backup", "once", success=False)
        assert blocked.returncode == 75 and "already_running" in blocked.stderr
        assert bundles() == before
        invoke(["docker", "rm", "-f", holder], environment)
        print("Concurrent backup is rejected without changing the previous backups", flush=True)

        wrong = dict(environment, DB_BACKUP_PASSWORD=secrets.token_urlsafe(32))
        failure = compose("run", "--rm", "backup", "once", env=wrong, success=False)
        assert failure.returncode != 0 and "stage=source_check" in failure.stderr
        assert all(value not in failure.stdout + failure.stderr for value in passwords.values())
        assert bundles() == before
        assert compose("run", "--rm", "backup", "health", success=False).returncode != 0
        # Also reject warnings even if pg_dump itself returns success. Diagnostics stay off logs.
        warning_script = r"""
set -eu
# The container's /tmp is noexec; an exported shell function avoids an executable fixture.
timeout() {
    if [[ $2 == pg_dump ]]; then
        echo WRAPPER_USED
        echo synthetic-diagnostic >&2
    fi
    command timeout "$@"
}
export -f timeout
exec bash /opt/scrap-backup/worker.sh once
"""
        warning = compose(
            "run",
            "--rm",
            "--user",
            "postgres",
            "--entrypoint",
            "bash",
            "backup",
            "-c",
            warning_script,
            success=False,
        )
        assert "WRAPPER_USED" in warning.stdout, "Warning injection did not execute"
        assert warning.returncode != 0, "Warning was treated as success"
        assert "synthetic-diagnostic" not in warning.stderr, "Raw diagnostic was printed"
        assert bundles() == before
        compose("run", "--rm", "backup", "once")
        compose("run", "--rm", "backup", "health")
        print(
            "Failure/warning preserves good backups, marks failure and recovers on success",
            flush=True,
        )

        compose("up", "-d", "--wait", "--wait-timeout", "60", "backup")
        for _ in range(20):
            names = bundles()
            if any(int(Path(name).name.split("-")[1]) >= 6 for name in names):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Periodic backup did not run twice")
        compose("stop", "backup")
        latest = bundles()[-1]
        checked = compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "bash",
            "backup",
            "-c",
            f"cd {latest}; sha256sum -c database.dump.sha256",
        )
        assert checked.returncode == 0
        # Stream the archive into a different DB with no plaintext credentials in argv/files.
        archive = subprocess.run(
            [
                *base,
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "cat",
                "backup",
                latest + "/database.dump",
            ],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            timeout=60,
        )
        assert archive.returncode == 0 and archive.stdout.startswith(b"PGDMP")
        compose(
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "createdb",
            "--template=template0",
            "--owner=scrap_owner",
            "backup_restore_check",
        )
        recovery = subprocess.run(
            [
                *base,
                "exec",
                "-T",
                "--user",
                "postgres",
                "postgres",
                "pg_restore",
                "--no-password",
                "--no-owner",
                "--no-privileges",
                "--role=scrap_owner",
                "--single-transaction",
                "--exit-on-error",
                "--dbname=backup_restore_check",
            ],
            input=archive.stdout,
            env=environment,
            cwd=ROOT,
            capture_output=True,
            timeout=60,
        )
        assert recovery.returncode == 0, "Scheduled archive could not be restored"
        restored_env = dict(
            environment,
            DATABASE_URL="postgresql+asyncpg://scrap_owner:"
            + quote(passwords["OWNER"], safe="")
            + "@postgres:5432/backup_restore_check",
        )
        compose(
            "run",
            "--rm",
            "--volume",
            mount,
            "-e",
            "DATABASE_URL",
            "manage",
            "-m",
            "src.app.cli.transfer",
            "verify",
            "--source",
            "/migration/source.db",
            env=restored_env,
        )
        print(
            "Periodic execution and isolated restore match every column in all 16 tables",
            flush=True,
        )
    finally:
        invoke(["docker", "rm", "-f", holder], environment, success=False)
        compose("--profile", "backup", "down", "--volumes", "--remove-orphans")
        invoke(["docker", "volume", "rm", volume], environment)
    print("Scheduled backups and SQLite WAL recovery passed; synthetic resources removed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="scheduled-backup-check-", dir=ROOT.parent
    ) as directory:
        verify(args.image, Path(directory))


if __name__ == "__main__":
    main()
