"""Add future setting-change history without manufacturing historical changes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_settings_history"
down_revision = "0004_minute_history_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings_history",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("actor_display_name", sa.String(100), nullable=False),
        sa.Column("pit_id", sa.String(50), nullable=False),
        sa.Column("resource", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(10), nullable=False),
        sa.Column(
            "before",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=True,
        ),
        sa.Column("after", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("changed_fields", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.CheckConstraint(
            "resource IN ('alert_settings', 'pit_config')", name="ck_settings_history_resource"
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'update')", name="ck_settings_history_operation"
        ),
    )
    op.create_index("ix_settings_history_pit_id", "settings_history", ["pit_id", "id"])
    op.create_index("ix_settings_history_actor_id", "settings_history", ["actor_username", "id"])
    op.create_index("ix_settings_history_time", "settings_history", ["occurred_at"])


def downgrade() -> None:
    raise RuntimeError("Automatic history downgrade is disabled; restore a verified backup")
