"""Add compact projections without pruning or rewriting existing source records."""

import sqlalchemy as sa
from alembic import op

revision = "0002_compact_observations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "edge_v2_messages",
        sa.Column("compact", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("edge_v2_artifacts", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_v2_legacy_history",
        "edge_v2_messages",
        ["site_id", "edge_id", "message_type", "compact", "generated_at"],
    )
    op.create_index(
        "ix_v2_status_sequence",
        "edge_v2_messages",
        ["site_id", "edge_id", "message_type", "instance_id", "sequence", "id"],
    )
    op.add_column(
        "edge_v2_operations", sa.Column("critical_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "edge_v2_latest",
        sa.Column("site_id", sa.String(128), primary_key=True),
        sa.Column("edge_id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "edge_v2_filters",
        sa.Column("site_id", sa.String(128), primary_key=True),
        sa.Column("edge_id", sa.String(128), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "edge_v2_minutes",
        sa.Column("site_id", sa.String(128), primary_key=True),
        sa.Column("edge_id", sa.String(128), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "edge_v2_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("edge_id", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_v2_transition_source", "edge_v2_transitions", ["site_id", "edge_id", "occurred_at"]
    )


def downgrade() -> None:
    raise RuntimeError("Automatic projection downgrade is disabled; restore a verified backup")
