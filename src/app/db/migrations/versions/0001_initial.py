"""Register the reviewed initial schema without replacing existing records."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from src.app.db.migrations.baseline import metadata
from src.app.db.schema_guard import validate_schema

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # This is an immutable snapshot, not the application's future model metadata.
    validate_schema(connection, metadata)
    metadata.create_all(connection, checkfirst=True)
    for table in metadata.sorted_tables:
        for index in table.indexes:
            index.create(connection, checkfirst=True)
    policy = metadata.tables["alert_policies"]
    pit = metadata.tables["pit_configs"]
    now = datetime.now(UTC)
    source = sa.select(
        pit.c.id,
        sa.literal(True),
        sa.literal(0),
        sa.literal(30),
        sa.literal(3),
        sa.literal(True),
        sa.literal(
            ["collection_required", "measurement_error", "device_error"],
            type_=policy.c.event_types.type,
        ),
        sa.literal(1),
        sa.literal(now, type_=sa.DateTime(timezone=True)),
        sa.literal(now, type_=sa.DateTime(timezone=True)),
    ).where(~sa.exists().where(policy.c.pit_id == pit.c.id))
    connection.execute(
        policy.insert().from_select(
            [
                "pit_id",
                "pre_alert_enabled",
                "send_delay_minutes",
                "repeat_interval_minutes",
                "maximum_repeat_count",
                "recovery_notification_enabled",
                "event_types",
                "version_id",
                "updated_at",
                "created_at",
            ],
            source,
        )
    )
    validate_schema(connection, metadata, complete=True)


def downgrade() -> None:
    raise RuntimeError("Automatic baseline downgrade is disabled; restore a verified backup")
