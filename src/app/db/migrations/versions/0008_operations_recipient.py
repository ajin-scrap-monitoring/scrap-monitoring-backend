"""Opt in existing recipients to operations email; preserve all business subscriptions."""

import sqlalchemy as sa
from alembic import op

revision = "0008_operations_recipient"
down_revision = "0007_raw_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_recipients",
        sa.Column(
            "operations_email_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM notification_recipients WHERE operations_email_enabled")
    ):
        raise RuntimeError("Disable operations subscriptions before a reviewed rollback")
    op.drop_column("notification_recipients", "operations_email_enabled")
