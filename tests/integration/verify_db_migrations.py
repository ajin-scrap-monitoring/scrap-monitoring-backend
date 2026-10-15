"""Explicit local Docker test of fresh and legacy PostgreSQL schema migrations."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic import command
from asyncpg import InvalidPasswordError
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.db.migration import (
    current_revisions,
    migration_config,
    require_current_schema,
    upgrade_schema,
)
from src.app.db.migrations.baseline import metadata as initial_metadata
from src.app.db.schema_guard import SchemaStateError
from src.app.models.alert_policy import AlertPolicy
from src.app.models.auth import AuthSession, AuthUser
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip()


def scram_verifier(password: str) -> str:
    """Build a salted PostgreSQL verifier; never persist the original password."""
    salt = secrets.token_bytes(16)
    iterations = 4096
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")

    def encoded(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{encoded(salt)}${encoded(stored_key)}:{encoded(server_key)}"


async def verify(port: int, password: str) -> None:
    url = URL.create(
        "postgresql+asyncpg",
        username="postgres",
        password=password,
        host="127.0.0.1",
        port=port,
        database="migration_check",
    )
    engine = create_async_engine(url)
    try:
        for _ in range(60):
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL readiness timed out")
        async with engine.connect() as connection:
            verifier = await connection.scalar(
                text("SELECT rolpassword FROM pg_authid WHERE rolname = current_user")
            )
            assert verifier.startswith("SCRAM-SHA-256$") and password not in verifier
        rejected = create_async_engine(url.set(password=secrets.token_urlsafe(24)))
        try:
            try:
                async with rejected.connect():
                    raise AssertionError("Incorrect password was accepted")
            except InvalidPasswordError:
                pass
        finally:
            await rejected.dispose()
        print("PostgreSQL SCRAM storage and incorrect-password rejection passed", flush=True)
        async with engine.begin() as connection:
            await connection.run_sync(upgrade_schema)
            await connection.run_sync(require_current_schema)
            await connection.run_sync(upgrade_schema)

            def compare(conn: Connection) -> None:
                config = migration_config()
                config.attributes["connection"] = conn
                command.check(config)

            await connection.run_sync(compare)
        print("Fresh PostgreSQL: schema, repeat upgrade and model comparison passed", flush=True)
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text("CREATE DATABASE legacy_check"))
    finally:
        await engine.dispose()

    legacy = create_async_engine(url.set(database="legacy_check"))
    try:
        tables = [
            t
            for t in initial_metadata.sorted_tables
            if t.name
            not in (
                "alert_policies",
                "notification_reads",
            )
        ]
        async with legacy.begin() as connection:
            await connection.run_sync(lambda conn: initial_metadata.create_all(conn, tables=tables))
        factory = async_sessionmaker(legacy, expire_on_commit=False)
        now = datetime.now(UTC)
        async with factory() as db:
            db.add(
                PitConfig(
                    id="pit-synthetic",
                    name="Synthetic pit",
                    warning_threshold_percent=72,
                    critical_threshold_percent=93,
                )
            )
            db.add(
                AuthUser(
                    username="synthetic",
                    display_name="Synthetic account",
                    role="viewer",
                    password_hash="synthetic-preserved-hash",
                )
            )
            db.add(
                AlertLog(
                    id=42,
                    pit_id="pit-synthetic",
                    triggered_at=now,
                    fill_ratio_percent=95,
                    message="Synthetic preserved alert",
                    read_at=now,
                )
            )
            await db.flush()
            db.add(
                AuthSession(
                    token_hash="a" * 64,
                    username="synthetic",
                    credential_version=1,
                    csrf_token="synthetic-csrf",
                    expires_at=now + timedelta(days=1),
                )
            )
            await db.commit()
        async with legacy.begin() as connection:
            try:
                await connection.run_sync(upgrade_schema)
            except SchemaStateError:
                pass
            else:
                raise AssertionError("Legacy DB must require explicit adoption")
            await connection.run_sync(upgrade_schema, True)
            await connection.run_sync(require_current_schema)
            assert await connection.run_sync(current_revisions) == ("0002_compact_observations",)
        async with factory() as db:
            assert (await db.get(PitConfig, "pit-synthetic")).critical_threshold_percent == 93
            assert (await db.get(AuthUser, "synthetic")).password_hash == "synthetic-preserved-hash"
            assert (await db.get(AuthSession, "a" * 64)).csrf_token == "synthetic-csrf"
            assert (await db.get(AlertLog, 42)).read_at is not None
            policy = await db.get(AlertPolicy, "pit-synthetic")
            assert policy.repeat_interval_minutes == 30 and len(policy.event_types) == 3
        print(
            "Legacy PostgreSQL: guard, adoption, JSONB backfill and data preservation passed",
            flush=True,
        )
    finally:
        await legacy.dispose()


def main() -> None:
    name = "db-migration-check-" + uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    # Docker receives only the one-way verifier, never the plaintext credential.
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--cpus",
        "0.5",
        "--memory",
        "512m",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_PASSWORD={scram_verifier(password)}",
        "-e",
        "POSTGRES_DB=migration_check",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
        "postgres:17-alpine",
    )
    try:
        port = int(docker("port", container, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
        asyncio.run(verify(port, password))
    finally:
        docker("rm", "-f", "-v", container)
    print("PostgreSQL migration checks passed; temporary DB removed")


if __name__ == "__main__":
    main()
