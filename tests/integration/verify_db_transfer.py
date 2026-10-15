"""Explicit synthetic SQLite-to-PostgreSQL transfer verification using local Docker."""

import asyncio
import hashlib
import os
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.db.base import Base
from src.app.db.migration import upgrade_schema
from src.app.db.schema_guard import SchemaStateError
from src.app.db.session import get_db_session
from src.app.db.transfer import TransferError, restart_sequences, transfer_database
from src.app.main import create_app
from src.app.models.log import AlertLog
from tests.integration.verify_db_migrations import docker, scram_verifier
from tests.transfer_fixtures import COOKIE, PASSWORD, seed_transfer_source

ROOT = Path(__file__).resolve().parents[2]


async def empty(engine):
    async with engine.connect() as db:
        for table in Base.metadata.sorted_tables:
            assert await db.scalar(select(func.count()).select_from(table)) == 0, table.name


async def verify(url, directory):
    source = directory / "synthetic-source.db"
    preserved_hash = await seed_transfer_source(source)
    original = hashlib.sha256(source.read_bytes()).digest()
    admin = create_async_engine(url)
    try:
        for _ in range(60):
            try:
                async with admin.connect() as db:
                    await db.execute(text("SELECT 1"))
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL readiness timed out")
        for name in ("transfer_check", "failure_check", "mismatch_check", "sequence_check"):
            async with admin.connect() as db:
                await db.execution_options(isolation_level="AUTOCOMMIT")
                await db.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    async def prepared(name):
        target = url.set(database=name)
        engine = create_async_engine(target)
        async with engine.begin() as db:
            await db.run_sync(upgrade_schema)
        return target, engine

    target, engine = await prepared("transfer_check")
    try:
        environment = dict(os.environ, DATABASE_URL=target.render_as_string(hide_password=False))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "src.app.cli.transfer",
            "check",
            "--source",
            str(source),
            env=environment,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b"all changes rolled back" in stdout
        assert target.password.encode() not in stdout + stderr
        await empty(engine)
        await transfer_database(source, target, mode="apply", batch_size=1)
        summaries = await transfer_database(source, target, mode="verify", batch_size=2)
        assert len(summaries) == 16 and summaries["media_clip_jobs"].rows == 2
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT password_hash FROM auth_users")) == preserved_hash
            assert await db.scalar(text("SELECT sequence FROM edge_v2_messages")) == 2**54 + 9
            assert await db.scalar(text("SELECT version_id FROM alert_policies")) == 7
            assert (
                await db.scalar(text("SELECT count(*) FROM media_clip_jobs WHERE snapshot IS NULL"))
                == 1
            )
        try:
            await transfer_database(source, target, mode="apply")
        except TransferError as error:
            assert "contains data" in str(error)
        else:
            raise AssertionError("Repeat apply must refuse a populated DB")
        await transfer_database(source, target, mode="verify")
        async with engine.begin() as db:
            await db.execute(text("UPDATE pit_configs SET name = 'synthetic-changed'"))
        try:
            await transfer_database(source, target, mode="verify")
        except TransferError as error:
            assert "comparison failed" in str(error)
        else:
            raise AssertionError("Same-count changed data must fail comparison")
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
            login = await client.post(
                "/api/v1/session", json={"username": "transfer-user", "password": PASSWORD}
            )
            assert login.status_code == 200
            notifications = await client.get("/api/v1/notifications")
            assert notifications.status_code == 200 and notifications.json()["unreadCount"] == 0
        async with engine.connect() as db:
            for table in Base.metadata.sorted_tables:
                column = table.autoincrement_column
                if column is not None:
                    expected = (await db.scalar(select(func.max(column)))) + 1
                    actual = await db.scalar(
                        text("SELECT nextval(pg_get_serial_sequence(:table_name, :column_name))"),
                        {"table_name": table.name, "column_name": column.name},
                    )
                    assert actual == expected, table.name
        async with factory() as db:
            log = AlertLog(
                pit_id="pit-synthetic",
                triggered_at=datetime.now(UTC),
                fill_ratio_percent=95,
                message="Synthetic post-transfer alert",
            )
            db.add(log)
            await db.commit()
            assert log.id == 44
        print(
            "Trial/apply/verify, all tables, credentials/session/read state and next ID passed",
            flush=True,
        )
    finally:
        await engine.dispose()

    target, engine = await prepared("failure_check")
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "CREATE FUNCTION reject_transfer() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'synthetic failure'; END $$"
                )
            )
            await db.execute(
                text(
                    "CREATE TRIGGER reject_transfer BEFORE INSERT ON notification_reads "
                    "FOR EACH ROW EXECUTE FUNCTION reject_transfer()"
                )
            )
        try:
            await transfer_database(source, target, mode="apply")
        except TransferError:
            pass
        else:
            raise AssertionError("Late insert failure must roll back")
        await empty(engine)
        async with engine.begin() as db:
            await db.execute(text("DROP TRIGGER reject_transfer ON notification_reads"))
        await transfer_database(source, target, mode="apply")
        print("Late insertion failure rolls back all tables and retry succeeds", flush=True)
    finally:
        await engine.dispose()

    target, engine = await prepared("mismatch_check")
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "CREATE FUNCTION change_transfer() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN NEW.name = 'synthetic-modified'; RETURN NEW; END $$"
                )
            )
            await db.execute(
                text(
                    "CREATE TRIGGER change_transfer BEFORE INSERT ON pit_configs "
                    "FOR EACH ROW EXECUTE FUNCTION change_transfer()"
                )
            )
        try:
            await transfer_database(source, target, mode="apply")
        except TransferError as error:
            assert "comparison failed" in str(error)
        else:
            raise AssertionError("Silent transformed data must fail before commit")
        await empty(engine)
        print("Value mismatch prevents commit even when row counts match", flush=True)
    finally:
        await engine.dispose()

    target, engine = await prepared("sequence_check")
    try:

        async def failed_after_sequences(db):
            await restart_sequences(db)
            raise TransferError("Synthetic failure after ID restart")

        with patch("src.app.db.transfer.restart_sequences", failed_after_sequences):
            try:
                await transfer_database(source, target, mode="apply")
            except TransferError:
                pass
            else:
                raise AssertionError("Post-sequence failure must roll back")
        await empty(engine)
        async with engine.connect() as db:
            state = (
                await db.execute(text("SELECT last_value, is_called FROM alert_logs_id_seq"))
            ).one()
            assert state.last_value == 1 and state.is_called is False
        broken = directory / "orphan.db"
        await seed_transfer_source(broken)
        sqlite = create_async_engine(f"sqlite+aiosqlite:///{broken}")
        try:
            async with sqlite.begin() as db:
                await db.execute(text("UPDATE notification_reads SET username = 'missing-account'"))
        finally:
            await sqlite.dispose()
        try:
            await transfer_database(broken, target, mode="apply")
        except TransferError as error:
            assert "foreign-key check" in str(error)
        else:
            raise AssertionError("Orphan reference must be rejected")
        await empty(engine)
        sqlite = create_async_engine(f"sqlite+aiosqlite:///{broken}")
        try:
            async with sqlite.begin() as db:
                await db.execute(text("DROP TABLE alembic_version"))
        finally:
            await sqlite.dispose()
        try:
            await transfer_database(broken, target, mode="apply")
        except SchemaStateError:
            pass
        else:
            raise AssertionError("Unversioned source must be rejected")
        await empty(engine)
        print("Sequence rollback, orphan FK and unversioned-source refusal passed", flush=True)
    finally:
        await engine.dispose()
    assert hashlib.sha256(source.read_bytes()).digest() == original
    print("Original SQLite bytes unchanged", flush=True)


def main():
    password = secrets.token_urlsafe(24)
    name = "db-transfer-check-" + uuid4().hex[:8]
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
        "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
        "postgres:17-alpine",
    )
    try:
        port = int(docker("port", container, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
        url = URL.create(
            "postgresql+asyncpg",
            username="postgres",
            password=password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        )
        with tempfile.TemporaryDirectory(prefix="transfer-check-", dir=ROOT.parent) as directory:
            asyncio.run(verify(url, Path(directory)))
    finally:
        docker("rm", "-f", "-v", container)
    print("PostgreSQL transfer checks passed; temporary DBs removed")


if __name__ == "__main__":
    main()
