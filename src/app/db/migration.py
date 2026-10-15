"""Alembic helpers called through AsyncConnection.run_sync; no nested event loops."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from src.app.db.schema_guard import SchemaStateError, application_tables, validate_schema


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def current_revisions(connection: Connection) -> tuple[str, ...]:
    return MigrationContext.configure(connection).get_current_heads()


def require_current_schema(connection: Connection) -> None:
    from src.app import models

    expected = tuple(ScriptDirectory.from_config(migration_config()).get_heads())
    if set(current_revisions(connection)) != set(expected):
        raise SchemaStateError("DB migration required; run python -m src.app.cli.db upgrade")
    validate_schema(connection, models.AuthUser.metadata, complete=True)


def guard_upgrade(connection: Connection, adopt_legacy: bool) -> None:
    if not current_revisions(connection) and application_tables(connection):
        if not adopt_legacy:
            raise SchemaStateError(
                "Unversioned DB: back up first, then run db upgrade --adopt-legacy"
            )
        from src.app.db.migrations.baseline import metadata

        validate_schema(connection, metadata)


def upgrade_schema(connection: Connection, adopt_legacy: bool = False) -> None:
    config = migration_config()
    config.attributes["connection"] = connection
    config.attributes["adopt_legacy"] = adopt_legacy
    command.upgrade(config, "head")


def initialize_schema(connection: Connection, *, allow_empty: bool) -> None:
    if not current_revisions(connection) and not application_tables(connection) and allow_empty:
        upgrade_schema(connection)
    require_current_schema(connection)
