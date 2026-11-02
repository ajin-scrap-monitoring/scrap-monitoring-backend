"""Add an optional small history summary without rewriting existing observations."""

import sqlalchemy as sa
from alembic import op

revision = "0004_minute_history_summary"
down_revision = "0003_event_export_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("edge_v2_minutes", sa.Column("history_summary", sa.JSON(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Automatic history downgrade is disabled; restore a verified backup")
