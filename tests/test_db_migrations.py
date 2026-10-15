"""Versioned schema creation, guarded adoption and preservation of existing data."""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.db.base import Base
from src.app.db.migration import (
    current_revisions,
    initialize_schema,
    migration_config,
    require_current_schema,
    upgrade_schema,
)
from src.app.db.schema_guard import SchemaStateError, application_tables
from src.app.models.alert_policy import AlertPolicy
from src.app.models.auth import AuthSession, AuthUser
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.log import AlertLog
from src.app.models.notification_read import NotificationRead


@pytest.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}")
    yield engine
    await engine.dispose()


async def test_empty_database_upgrade_matches_models_and_is_repeatable(engine):
    async with engine.begin() as connection:
        await connection.run_sync(upgrade_schema)
        await connection.run_sync(require_current_schema)
        assert await connection.run_sync(current_revisions) == ("0001_initial",)
        assert await connection.run_sync(application_tables) == set(Base.metadata.tables)
        await connection.run_sync(upgrade_schema)

        def compare(conn):
            config = migration_config()
            config.attributes["connection"] = conn
            command.check(config)

        await connection.run_sync(compare)


async def legacy_db(engine, modern=False):
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if modern
        or table.name
        not in (
            "alert_policies",
            "notification_reads",
        )
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            PitConfig(
                id="pit-existing",
                name="Synthetic existing",
                total_depth_cm=400,
                warning_threshold_percent=72,
                critical_threshold_percent=93,
            )
        )
        db.add(
            AuthUser(
                username="existing",
                display_name="Synthetic existing",
                role="viewer",
                password_hash="synthetic-preserved-hash",
            )
        )
        db.add(
            AlertLog(
                id=42,
                pit_id="pit-existing",
                triggered_at=now,
                fill_ratio_percent=95,
                message="Synthetic preserved log",
                read_at=now,
            )
        )
        db.add(
            EdgeV2Message(
                site_id="site-demo",
                edge_id="edge-demo",
                message_id="synthetic-id",
                message_type="edge_status",
                config_revision="cfg-synthetic",
                generated_at=now,
                received_at=now,
                body_hash="a" * 64,
                payload={"synthetic": True},
            )
        )
        await db.flush()
        db.add(
            AuthSession(
                token_hash="b" * 64,
                username="existing",
                credential_version=1,
                csrf_token="synthetic-csrf",
                expires_at=now + timedelta(days=1),
            )
        )
        if modern:
            db.add(
                AlertPolicy(
                    pit_id="pit-existing",
                    pre_alert_enabled=False,
                    send_delay_minutes=9,
                    repeat_interval_minutes=45,
                    maximum_repeat_count=7,
                    recovery_notification_enabled=False,
                    event_types=["device_error"],
                    version_id=5,
                )
            )
        if modern:
            db.add(NotificationRead(username="existing", alert_id=42, read_at=now))
        await db.commit()
    return factory


@pytest.mark.parametrize("modern", [False, True])
async def test_adoption_preserves_legacy_data_and_existing_policy(engine, modern):
    factory = await legacy_db(engine, modern)
    async with engine.begin() as connection:
        await connection.run_sync(upgrade_schema, True)
        await connection.run_sync(require_current_schema)
    async with factory() as db:
        pit = await db.get(PitConfig, "pit-existing")
        assert (
            pit.total_depth_cm,
            pit.warning_threshold_percent,
            pit.critical_threshold_percent,
        ) == (400, 72, 93)
        assert (await db.get(AuthUser, "existing")).password_hash == "synthetic-preserved-hash"
        assert (await db.get(AuthSession, "b" * 64)).csrf_token == "synthetic-csrf"
        log = await db.get(AlertLog, 42)
        assert log.read_at is not None and log.message == "Synthetic preserved log"
        raw = await db.get(EdgeV2Message, 1)
        assert raw.payload == {"synthetic": True} and raw.body_hash == "a" * 64
        personal = await db.get(NotificationRead, ("existing", 42))
        assert (personal is not None) == modern
        policy = await db.get(AlertPolicy, "pit-existing")
        assert policy.send_delay_minutes == (9 if modern else 0)
        assert policy.version_id == (5 if modern else 1)
        assert policy.event_types == (
            ["device_error"]
            if modern
            else ["collection_required", "measurement_error", "device_error"]
        )
        db.add(
            AlertLog(
                pit_id="pit-existing",
                triggered_at=datetime.now(UTC),
                fill_ratio_percent=90,
                message="Synthetic next ID",
            )
        )
        await db.commit()
        assert (await db.get(AlertLog, 43)) is not None


async def test_unversioned_data_needs_explicit_adoption(engine):
    await legacy_db(engine)
    async with engine.begin() as connection:
        before = await connection.run_sync(application_tables)
        with pytest.raises(SchemaStateError, match="adopt-legacy"):
            await connection.run_sync(upgrade_schema)
        assert await connection.run_sync(application_tables) == before
        assert await connection.run_sync(current_revisions) == ()
        assert not await connection.run_sync(
            lambda conn: inspect(conn).has_table("alembic_version")
        )


@pytest.mark.parametrize(
    "damage", ["column", "unique", "unknown_table", "wrong_type", "foreign_key", "check"]
)
async def test_incompatible_schema_is_rejected_before_registration(engine, damage):
    await legacy_db(engine)
    async with engine.begin() as connection:
        if damage == "column":
            await connection.execute(text("ALTER TABLE pit_configs ADD COLUMN unexpected TEXT"))
        elif damage == "unique":
            await connection.execute(text("DROP INDEX ix_scrap_metrics_measurement_id"))
        elif damage == "unknown_table":
            await connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
        elif damage == "wrong_type":
            await connection.execute(text("DROP TABLE media_indices"))
            await connection.execute(text("CREATE TABLE media_indices (id TEXT PRIMARY KEY)"))
        elif damage == "foreign_key":
            await connection.execute(text("DROP TABLE auth_sessions"))
            await connection.execute(
                text(
                    "CREATE TABLE auth_sessions (token_hash VARCHAR(64) NOT NULL PRIMARY KEY, "
                    "username VARCHAR(64) NOT NULL, credential_version INTEGER NOT NULL, "
                    "csrf_token VARCHAR(64) NOT NULL, expires_at DATETIME NOT NULL, "
                    "created_at DATETIME NOT NULL)"
                )
            )
        else:
            await connection.execute(text("DROP TABLE auth_users"))
            await connection.execute(
                text(
                    "CREATE TABLE auth_users (username VARCHAR(64) NOT NULL PRIMARY KEY, "
                    "display_name VARCHAR(100) NOT NULL, role VARCHAR(20) NOT NULL, "
                    "password_hash VARCHAR(512) NOT NULL, is_active BOOLEAN NOT NULL, "
                    "credential_version INTEGER NOT NULL, created_at DATETIME NOT NULL)"
                )
            )
        before = await connection.run_sync(application_tables)
        with pytest.raises(SchemaStateError):
            await connection.run_sync(upgrade_schema, True)
        assert await connection.run_sync(application_tables) == before
        assert await connection.run_sync(current_revisions) == ()


async def test_missing_nonunique_index_is_restored_during_adoption(engine):
    await legacy_db(engine)
    async with engine.begin() as connection:
        await connection.execute(text("DROP INDEX ix_alert_logs_triggered_at"))
        await connection.run_sync(upgrade_schema, True)
        await connection.run_sync(require_current_schema)


async def test_application_initialization_only_creates_empty_nonproduction_db(engine):
    async with engine.begin() as connection:
        with pytest.raises(SchemaStateError):
            await connection.run_sync(initialize_schema, allow_empty=False)
        assert await connection.run_sync(application_tables) == set()
        await connection.run_sync(initialize_schema, allow_empty=True)
        await connection.run_sync(initialize_schema, allow_empty=False)


async def test_application_does_not_auto_adopt_legacy_db(engine):
    await legacy_db(engine)
    async with engine.begin() as connection:
        with pytest.raises(SchemaStateError):
            await connection.run_sync(initialize_schema, allow_empty=True)
        assert await connection.run_sync(current_revisions) == ()


async def test_unknown_revision_or_missing_table_blocks_startup(engine):
    async with engine.begin() as connection:
        await connection.run_sync(upgrade_schema)
        await connection.execute(text("UPDATE alembic_version SET version_num='unknown_revision'"))
        with pytest.raises(SchemaStateError):
            await connection.run_sync(require_current_schema)
        await connection.execute(text("UPDATE alembic_version SET version_num='0001_initial'"))
        await connection.execute(text("DROP TABLE notification_reads"))
        with pytest.raises(SchemaStateError):
            await connection.run_sync(require_current_schema)


async def test_baseline_downgrade_refuses_to_drop_data(engine):
    factory = await legacy_db(engine)
    async with engine.begin() as connection:
        await connection.run_sync(upgrade_schema, True)

        def downgrade(conn):
            config = migration_config()
            config.attributes["connection"] = conn
            command.downgrade(config, "base")

        with pytest.raises(RuntimeError, match="backup"):
            await connection.run_sync(downgrade)
    async with factory() as db:
        assert (await db.get(AlertLog, 42)).message == "Synthetic preserved log"


async def test_second_adoption_does_not_reset_policy_or_read_state(engine):
    factory = await legacy_db(engine, modern=True)
    async with engine.begin() as connection:
        await connection.run_sync(upgrade_schema, True)
        await connection.run_sync(upgrade_schema, True)
    async with factory() as db:
        assert (await db.get(AlertPolicy, "pit-existing")).version_id == 5
        assert (await db.get(AlertLog, 42)).read_at is not None


async def cli(url, *args):
    env = {**os.environ, "DATABASE_URL": url, "APP_ENV": "test"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.app.cli.db",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    return process.returncode, stdout.decode(), stderr.decode()


async def test_cli_new_database_upgrade_status_and_check(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    code, output, _ = await cli(url, "current")
    assert code == 0 and "unversioned" in output
    assert (await cli(url, "check"))[0] == 1
    assert (await cli(url, "upgrade"))[0] == 0
    assert (await cli(url, "check"))[0] == 0
    assert "0001_initial" in (await cli(url, "current"))[1]
    assert (await cli(url, "upgrade"))[0] == 0


async def test_cli_legacy_adoption_requires_explicit_flag(engine):
    await legacy_db(engine)
    url = str(engine.url)
    code, _, error = await cli(url, "upgrade")
    assert code == 1 and "adopt-legacy" in error
    assert (await cli(url, "upgrade", "--adopt-legacy"))[0] == 0
    assert (await cli(url, "check"))[0] == 0
    assert (await cli(url, "current", "--adopt-legacy"))[0] == 2


async def test_cli_connection_failure_does_not_expose_credentials():
    code, output, error = await cli(
        "postgresql+asyncpg://synthetic-user:must-not-leak@127.0.0.1:1/synthetic", "check"
    )
    assert code == 1 and "failed" in error
    assert "must-not-leak" not in output + error and "Traceback" not in error
