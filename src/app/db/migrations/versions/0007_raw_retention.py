"""Index raw-expiration candidates without rewriting or deleting stored payloads."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

from src.app.db.migrations.raw_retention_v1 import ARTIFACT_INDEX, MESSAGE_INDEX, retention_sql

revision = "0007_raw_retention"
down_revision = "0006_email_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    sql = retention_sql(dialect)
    definitions = [
        (ARTIFACT_INDEX, "edge_v2_artifacts", sql.artifact_where),
        (MESSAGE_INDEX, "edge_v2_messages", sql.message_where),
    ]

    def create_indexes(bind: Connection, *, concurrent_build: bool) -> None:
        for name, table, predicate in definitions:
            # Only these new auxiliary names are replaced on a retry. Existing
            # application indexes and every data row remain untouched.
            if dialect == "postgresql":
                owner = bind.scalar(
                    sa.text(
                        "SELECT tablename FROM pg_indexes WHERE schemaname=current_schema() "
                        "AND indexname=:name"
                    ),
                    {"name": name},
                )
                if owner is not None and owner != table:
                    raise RuntimeError("Raw retention index name belongs to another table")
            concurrent = " CONCURRENTLY" if concurrent_build else ""
            bind.execute(sa.text(f"DROP INDEX{concurrent} IF EXISTS {name}"))
            columns = (
                f"{sql.artifact_day}, id"
                if name == ARTIFACT_INDEX and dialect == "postgresql"
                else "kind, id"
                if name == ARTIFACT_INDEX
                else "received_at, id"
            )
            bind.execute(
                sa.text(f"CREATE INDEX{concurrent} {name} ON {table} ({columns}) WHERE {predicate}")
            )

    bind = op.get_bind()
    if dialect != "postgresql":
        create_indexes(bind, concurrent_build=False)
        return
    wrote_tables = bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
            "AND relation IN ('edge_v2_artifacts'::regclass,'edge_v2_messages'::regclass) "
            "AND mode IN ('RowExclusiveLock','ShareRowExclusiveLock',"
            "'ExclusiveLock','AccessExclusiveLock') AND granted)"
        )
    )
    empty = all(
        not bind.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table} LIMIT 1)"))
        for table in ("edge_v2_artifacts", "edge_v2_messages")
    )
    if wrote_tables or empty:
        # Fresh tables and migrations already holding write/DDL locks cannot be
        # observed by a separate connection. Build within that existing transaction.
        create_indexes(bind, concurrent_build=False)
        return
    valid = set(
        bind.scalars(
            sa.text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_index i ON i.indexrelid=c.oid "
                "WHERE n.nspname=current_schema() AND i.indisvalid"
            )
        )
    )
    if not {ARTIFACT_INDEX, MESSAGE_INDEX} <= valid:
        raise RuntimeError(
            "Populated PostgreSQL requires index preparation outside a transaction; "
            "use python -m src.app.cli.db upgrade"
        )


def downgrade() -> None:
    raise RuntimeError("Raw expiration cannot restore removed data; use a verified backup")
