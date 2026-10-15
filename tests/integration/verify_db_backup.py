"""Restore a synthetic pg_dump after removing the original PostgreSQL container/volume."""

import asyncio
import hashlib
import os
import secrets
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.db.base import Base
from src.app.db.migration import current_revisions, require_current_schema, upgrade_schema
from src.app.db.session import get_db_session
from src.app.db.transfer import summarize, transfer_database
from src.app.main import create_app
from src.app.models.log import AlertLog
from tests.integration.verify_db_migrations import docker, scram_verifier
from tests.transfer_fixtures import COOKIE, PASSWORD, seed_transfer_source

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "backup_check"


@contextmanager
def postgres():
    """Only generated local containers are accepted; no production URL input."""
    password = secrets.token_urlsafe(24)
    name = "db-backup-check-" + uuid4().hex[:8]
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--cpus",
        "1",
        "--memory",
        "512m",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_PASSWORD={scram_verifier(password)}",
        "-e",
        f"POSTGRES_DB={DATABASE}",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
        "postgres:17-alpine",
    )
    try:
        port = int(docker("port", container, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
        yield (
            container,
            URL.create(
                "postgresql+asyncpg",
                username="postgres",
                password=password,
                host="127.0.0.1",
                port=port,
                database=DATABASE,
            ),
        )
    finally:
        docker("rm", "-f", "-v", container)


async def ready(url):
    engine = create_async_engine(url, hide_parameters=True)
    try:
        for _ in range(60):
            try:
                async with engine.connect() as db:
                    await db.execute(text("SELECT 1"))
                return
            except Exception:
                await asyncio.sleep(0.5)
        raise RuntimeError("PostgreSQL readiness timed out")
    finally:
        await engine.dispose()


async def snapshot(url):
    """Compare every column without printing hashes, sessions or row values."""
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            await db.execution_options(isolation_level="REPEATABLE READ")
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.run_sync(require_current_schema)
            revisions = await db.run_sync(current_revisions)
            summaries = {
                table.name: await summarize(db, table, 2) for table in Base.metadata.sorted_tables
            }
            sequences = {}
            for table in Base.metadata.sorted_tables:
                column = table.autoincrement_column
                if column is None:
                    continue
                sequence = await db.scalar(
                    text("SELECT pg_get_serial_sequence(:table, :column)"),
                    {"table": table.name, "column": column.name},
                )
                assert sequence is not None, table.name
                # Names come from PostgreSQL, not external command-line arguments.
                state = (
                    await db.execute(text(f"SELECT last_value, is_called FROM {sequence}"))
                ).one()
                sequences[table.name] = (state.last_value, state.is_called)
            return revisions, summaries, sequences
    finally:
        await engine.dispose()


async def prepare_source(url, source):
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.begin() as db:
            await db.run_sync(upgrade_schema)
        await transfer_database(source, url, mode="apply")
        async with engine.begin() as db:
            for index, table in enumerate(Base.metadata.sorted_tables):
                column = table.autoincrement_column
                if column is None:
                    continue
                sequence = await db.scalar(
                    text("SELECT pg_get_serial_sequence(:table, :column)"),
                    {"table": table.name, "column": column.name},
                )
                maximum = await db.scalar(select(func.max(column)))
                # Real DBs have unused IDs after rollbacks/deletions. Restoring
                # max(id)+1 is not equivalent to preserving a sequence backup.
                await db.execute(text(f"ALTER SEQUENCE {sequence} RESTART WITH {maximum + 20}"))
                if index % 2:
                    await db.scalar(text("SELECT nextval(:sequence)"), {"sequence": sequence})
    finally:
        await engine.dispose()


def dump(container, path):
    """Never publish a partial archive as a successful backup."""
    partial = path.with_suffix(".partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "postgres",
                    container,
                    "pg_dump",
                    "--no-password",
                    "--format=custom",
                    "--dbname",
                    DATABASE,
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        if result.returncode:
            raise RuntimeError("pg_dump failed; partial archive is not a backup")
        assert partial.stat().st_size > 0
        partial.rename(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        partial.unlink(missing_ok=True)


def restore(container, path):
    with path.open("rb") as archive:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "--user",
                "postgres",
                container,
                "pg_restore",
                "--no-password",
                "--dbname",
                DATABASE,
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--role=backup_restore",
            ],
            stdin=archive,
            capture_output=True,
            timeout=120,
        )
    # Do not echo pg_restore stderr: a real DB error can include row values.
    return result.returncode


async def assert_empty(url):
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
                == 0
            ), "Failed restore left partial tables"
    finally:
        await engine.dispose()


async def prepare_target(url):
    engine = create_async_engine(url, hide_parameters=True)
    password = secrets.token_urlsafe(24)
    try:
        async with engine.begin() as db:
            await db.execute(text("CREATE ROLE backup_restore NOLOGIN"))
            await db.execute(text("ALTER SCHEMA public OWNER TO backup_restore"))
            # Only a one-way verifier is sent in SQL; never the login password.
            verifier = scram_verifier(password)
            await db.execute(
                text("SELECT set_config('backup_check.scram', :verifier, true)"),
                {"verifier": verifier},
            )
            await db.execute(
                text(
                    "DO $$ BEGIN EXECUTE format('CREATE ROLE backup_app LOGIN PASSWORD %L', "
                    "current_setting('backup_check.scram')); END $$"
                )
            )
        return url.set(username="backup_app", password=password)
    finally:
        await engine.dispose()


async def grant_runtime(url):
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.begin() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                        "AND tableowner <> 'backup_restore'"
                    )
                )
                == 0
            ), "Restored tables did not receive the dedicated owner"
            await db.execute(text("GRANT USAGE ON SCHEMA public TO backup_app"))
            await db.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    "ON ALL TABLES IN SCHEMA public TO backup_app"
                )
            )
            await db.execute(
                text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_app")
            )
    finally:
        await engine.dispose()


async def verify_restored(url, expected):
    assert await snapshot(url) == expected, "Restored schema, data or sequences differ"
    assert len(expected[1]) == 16 and all(summary.rows for summary in expected[1].values())
    print("Alembic, all 16 tables/columns and exact sequence state restored", flush=True)
    engine = create_async_engine(url, hide_parameters=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app = create_app()

        async def session():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db_session] = session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            client.cookies.set("scrap_session", COOKIE)
            response = await client.get("/api/v1/session")
            assert response.status_code == 200 and response.json()["user"]["id"] == "transfer-user"
            notifications = await client.get("/api/v1/notifications")
            assert notifications.status_code == 200 and notifications.json()["unreadCount"] == 0
            login = await client.post(
                "/api/v1/session", json={"username": "transfer-user", "password": PASSWORD}
            )
            assert login.status_code == 200, "Restored Argon2id account cannot log in"
        async with engine.begin() as db:
            for table in Base.metadata.sorted_tables:
                column = table.autoincrement_column
                if column is None:
                    continue
                last, called = expected[2][table.name]
                actual = await db.scalar(
                    text("SELECT nextval(pg_get_serial_sequence(:table, :column))"),
                    {"table": table.name, "column": column.name},
                )
                assert actual == last + int(called), table.name
        # A real FK rejection proves constraints are active after the restore.
        async with engine.connect() as db:
            try:
                async with db.begin():
                    await db.execute(
                        text(
                            "INSERT INTO notification_reads "
                            "(username, alert_id, read_at, created_at) "
                            "VALUES ('missing-synthetic-user', 42, now(), now())"
                        )
                    )
            except IntegrityError as error:
                assert error.orig.sqlstate == "23503", "Expected a foreign-key violation"
            else:
                raise AssertionError("Restored foreign key did not reject an orphan")
            try:
                async with db.begin():
                    await db.execute(text("CREATE TABLE synthetic_forbidden (id integer)"))
            except ProgrammingError as error:
                assert error.orig.sqlstate == "42501", "Expected insufficient privileges"
            else:
                raise AssertionError("Runtime account unexpectedly has schema creation permission")
        async with factory() as db:
            original = await db.get(AlertLog, 42)
            last, called = expected[2]["alert_logs"]
            log = AlertLog(
                pit_id=original.pit_id,
                triggered_at=original.triggered_at,
                fill_ratio_percent=95,
                message="Synthetic post-restore write",
            )
            db.add(log)
            await db.commit()
            assert log.id == last + int(called) + 1
        print(
            "Restored login/session/read state, next IDs, FK and runtime permissions passed",
            flush=True,
        )
    finally:
        await engine.dispose()


def main():
    # This directory contains synthetic fixtures only, never an operational dump.
    with tempfile.TemporaryDirectory(prefix="backup-check-", dir=ROOT.parent) as directory:
        private = Path(directory)
        source = private / "synthetic-source.db"
        asyncio.run(seed_transfer_source(source))
        archive = private / "synthetic.dump"
        with postgres() as (container, url):
            asyncio.run(ready(url))
            asyncio.run(prepare_source(url, source))
            expected = asyncio.run(snapshot(url))
            dump(container, archive)
        print(
            "Custom archive created; original PostgreSQL container and volume removed", flush=True
        )
        checksum = hashlib.sha256(archive.read_bytes()).digest()
        damaged = private / "truncated.dump"
        descriptor = os.open(damaged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            data = archive.read_bytes()
            output.write(data[: len(data) * 3 // 4])
        with postgres() as (container, url):
            asyncio.run(ready(url))
            runtime = asyncio.run(prepare_target(url))
            assert restore(container, damaged) != 0, "Truncated archive was accepted"
            asyncio.run(assert_empty(url))
            print(
                "Truncated archive rejected; single transaction left no partial tables", flush=True
            )
            assert hashlib.sha256(archive.read_bytes()).digest() == checksum
            assert restore(container, archive) == 0, "Valid archive restore failed"
            asyncio.run(grant_runtime(url))
            asyncio.run(verify_restored(runtime, expected))
            # Restoring again must fail rather than silently merge or replace data.
            before_repeat = asyncio.run(snapshot(url))
            assert restore(container, archive) != 0, "Restore into populated DB was accepted"
            assert asyncio.run(snapshot(url)) == before_repeat, "Failed repeat restore changed data"
            print("Populated-target restore failed without changing existing data", flush=True)
        assert hashlib.sha256(archive.read_bytes()).digest() == checksum
    print("PostgreSQL backup/restore checks passed; temporary DBs and archives removed")


if __name__ == "__main__":
    main()
