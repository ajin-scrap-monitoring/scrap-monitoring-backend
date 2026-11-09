"""Add future email delivery receipts without backfilling historical notifications."""

import sqlalchemy as sa
from alembic import op

revision = "0006_email_deliveries"
down_revision = "0005_settings_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_episodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pit_id", sa.String(50), nullable=False),
        sa.Column("condition_id", sa.String(255), nullable=False),
        sa.Column("condition_token", sa.String(64), nullable=False),
        sa.Column("condition_state", sa.String(50), nullable=False),
        sa.Column("recipient_id", sa.String(50), nullable=False),
        sa.Column("address_digest", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("component_kind", sa.String(20), nullable=True),
        sa.Column("component_id", sa.String(128), nullable=True),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sent_count >= 0", name="ck_email_episode_count"),
    )
    op.create_index("ix_email_episodes_pit_phase", "email_episodes", ["pit_id", "phase"])
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("episode_id", sa.String(36), sa.ForeignKey("email_episodes.id"), nullable=True),
        sa.Column("recipient_id", sa.String(50), nullable=False),
        sa.Column("address_digest", sa.String(64), nullable=False),
        sa.Column("pit_id", sa.String(50), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.String(256), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("episode_id", "phase", "sequence", name="uq_email_episode_delivery"),
        sa.CheckConstraint("attempts >= 0 AND sequence >= 1", name="ck_email_delivery_count"),
    )
    op.create_index("ix_email_deliveries_due", "email_deliveries", ["status", "due_at"])
    op.create_index("ix_email_deliveries_attempt", "email_deliveries", ["attempted_at"])


def downgrade() -> None:
    raise RuntimeError("Automatic email receipt downgrade is disabled; restore a verified backup")
