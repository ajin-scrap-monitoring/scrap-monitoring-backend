"""Alembic helpers called through AsyncConnection.run_sync; no nested event loops."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.app.db.migrations.raw_retention_v1 import INDEX_NAMES
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
    # Auxiliary expiration indexes are migration-managed across the two dialects.
    if connection.dialect.name == "postgresql":
        names = set(
            connection.scalars(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_index i ON i.indexrelid=c.oid "
                    "WHERE n.nspname=current_schema() AND i.indisvalid"
                )
            )
        )
    else:
        names = set(connection.scalars(text("SELECT name FROM sqlite_master WHERE type='index'")))
    if not INDEX_NAMES <= names:
        raise SchemaStateError("Raw retention indexes missing; check database migration state")


def guard_upgrade(connection: Connection, adopt_legacy: bool) -> None:
    if not current_revisions(connection) and application_tables(connection):
        if not adopt_legacy:
            raise SchemaStateError(
                "Unversioned DB: back up first, then run db upgrade --adopt-legacy"
            )
        from src.app.db.migrations.baseline import metadata

        validate_schema(connection, metadata)


def upgrade_schema(
    connection: Connection, adopt_legacy: bool = False, revision: str = "head"
) -> None:
    config = migration_config()
    config.attributes["connection"] = connection
    config.attributes["adopt_legacy"] = adopt_legacy
    command.upgrade(config, revision)


def initialize_schema(connection: Connection, *, allow_empty: bool) -> None:
    if not current_revisions(connection) and not application_tables(connection) and allow_empty:
        upgrade_schema(connection)
    require_current_schema(connection)
