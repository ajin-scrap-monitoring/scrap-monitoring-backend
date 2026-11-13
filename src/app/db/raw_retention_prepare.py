"""Prepare populated PostgreSQL lookup indexes outside migration transactions."""

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.db.migration import current_revisions
from src.app.db.migrations.raw_retention_v1 import ARTIFACT_INDEX, MESSAGE_INDEX, retention_sql

PRE_RETENTION_REVISION = "0006_email_deliveries"


def build_postgresql_indexes(connection: Connection) -> None:
    sql = retention_sql("postgresql")
    definitions = [
        (ARTIFACT_INDEX, "edge_v2_artifacts", f"{sql.artifact_day}, id", sql.artifact_where),
        (MESSAGE_INDEX, "edge_v2_messages", "received_at, id", sql.message_where),
    ]
    for name, table, columns, predicate in definitions:
        owner = connection.scalar(
            text(
                "SELECT tablename FROM pg_indexes WHERE schemaname=current_schema() "
                "AND indexname=:name"
            ),
            {"name": name},
        )
        if owner is not None and owner != table:
            raise RuntimeError("Retention index name belongs to another table")
        # Retry only our new auxiliary names, including an interrupted invalid build.
        connection.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        connection.execute(
            text(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({columns}) WHERE {predicate}")
        )


async def prepare_retention_indexes(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql":
        return
    async with engine.connect() as connection:
        revisions = await connection.run_sync(current_revisions)
        await connection.rollback()
        if revisions != (PRE_RETENTION_REVISION,):
            return
        # No outer snapshot/transaction may remain: concurrent expression indexes
        # otherwise wait for the migration connection itself.
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.run_sync(build_postgresql_indexes)
