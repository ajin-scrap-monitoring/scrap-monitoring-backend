"""Explicit local Docker test of fresh and legacy PostgreSQL schema migrations."""

import asyncio
import secrets
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.db.base import Base
from src.app.db.migration import (
    current_revisions,
    migration_config,
    require_current_schema,
    upgrade_schema,
)
from src.app.db.schema_guard import SchemaStateError
from src.app.models.alert_policy import AlertPolicy
from src.app.models.auth import AuthSession, AuthUser
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog

ROOT = Path(__file__).resolve().parents[2]


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip()


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
            for t in Base.metadata.sorted_tables
            if t.name
            not in (
                "alert_policies",
                "notification_reads",
            )
        ]
        async with legacy.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
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
            assert await connection.run_sync(current_revisions) == ("0001_initial",)
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
    with tempfile.TemporaryDirectory(prefix="migration-check-", dir=ROOT.parent) as directory:
        env = Path(directory) / "postgres.env"
        password = secrets.token_urlsafe(24)
        env.touch(mode=0o600)
        env.write_text(f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=migration_check\n")
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
            "--env-file",
            str(env),
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
