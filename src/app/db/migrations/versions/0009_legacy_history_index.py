"""Restrict the auxiliary history index; preserve every receipt and unique key."""

from alembic import op
from sqlalchemy import text

from src.app.db.migrations.legacy_index_v1 import COLUMNS, INDEX, PREDICATE, STAGING, index_state

revision = "0009_legacy_history_index"
down_revision = "0008_operations_recipient"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        bind.execute(text(f"DROP INDEX {INDEX}"))
        bind.execute(
            text(f"CREATE INDEX {INDEX} ON edge_v2_messages ({COLUMNS}) WHERE {PREDICATE}")
        )
        return
    if index_state(bind, INDEX, True) != (True, True):
        raise RuntimeError("Original history index has an unexpected definition")
    state = index_state(bind, STAGING)
    if state != (True, True):
        # A fresh schema created in this transaction cannot be indexed by another
        # connection. Populated databases must use the concurrent CLI preparation.
        wrote = bind.scalar(
            text("""
            SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid()
            AND relation='edge_v2_messages'::regclass AND granted
            AND mode IN ('RowExclusiveLock','ShareRowExclusiveLock',
                         'ExclusiveLock','AccessExclusiveLock'))
        """)
        )
        empty = not bind.scalar(text("SELECT EXISTS(SELECT 1 FROM edge_v2_messages LIMIT 1)"))
        if state is not None or not (wrote or empty):
            raise RuntimeError(
                "Use python -m src.app.cli.db upgrade for concurrent index preparation"
            )
        bind.execute(
            text(f"CREATE INDEX {STAGING} ON edge_v2_messages ({COLUMNS}) WHERE {PREDICATE}")
        )
    bind.execute(text("SET LOCAL lock_timeout='5s'"))
    # Both operations and the revision update commit together; a timeout rolls
    # back the swap while the original index remains available.
    bind.execute(text(f"DROP INDEX {INDEX}"))
    bind.execute(text(f"ALTER INDEX {STAGING} RENAME TO {INDEX}"))


def downgrade() -> None:
    raise RuntimeError("Rebuilding a full receipt index requires a reviewed maintenance plan")
