"""Preserve request aliases without changing or recreating existing media jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0003_event_export_aliases"
down_revision = "0002_compact_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_export_aliases",
        sa.Column("owner", sa.String(64), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("media_clip_jobs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_export_aliases_job_id", "event_export_aliases", ["job_id"])


def downgrade() -> None:
    raise RuntimeError("Automatic alias downgrade is disabled; restore a verified backup")
