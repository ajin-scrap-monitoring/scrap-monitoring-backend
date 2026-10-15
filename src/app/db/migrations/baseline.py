"""Immutable initial schema snapshot (0001); never import live ORM models here."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

sa.Table(
    "alert_logs",
    metadata,
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("pit_id", sa.String(length=50), nullable=False),
    sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fill_ratio_percent", sa.Float(), nullable=False),
    sa.Column("channel", sa.String(length=30), nullable=False),
    sa.Column("status", sa.String(length=20), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
    sa.Column("event_type", sa.String(length=50), nullable=False),
    sa.Column("severity", sa.String(length=20), nullable=False),
    sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Index("ix_alert_logs_pit_id", metadata.tables["alert_logs"].c["pit_id"], unique=False)
sa.Index(
    "ix_alert_logs_triggered_at", metadata.tables["alert_logs"].c["triggered_at"], unique=False
)
sa.Table(
    "auth_users",
    metadata,
    sa.Column("username", sa.String(length=64), nullable=False),
    sa.Column("display_name", sa.String(length=100), nullable=False),
    sa.Column("role", sa.String(length=20), nullable=False),
    sa.Column("password_hash", sa.String(length=512), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False),
    sa.Column("credential_version", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("role IN ('administrator', 'viewer')", name="ck_auth_role"),
    sa.PrimaryKeyConstraint("username"),
)
sa.Table(
    "collection_logs",
    metadata,
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("pit_id", sa.String(length=50), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("residual_height_cm", sa.Float(), nullable=False),
    sa.Column("operator_name", sa.String(length=100), nullable=False),
    sa.Column("notes", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Index(
    "ix_collection_logs_completed_at",
    metadata.tables["collection_logs"].c["completed_at"],
    unique=False,
)
sa.Index(
    "ix_collection_logs_pit_completed",
    metadata.tables["collection_logs"].c["pit_id"],
    metadata.tables["collection_logs"].c["completed_at"],
    unique=False,
)
sa.Index("ix_collection_logs_pit_id", metadata.tables["collection_logs"].c["pit_id"], unique=False)
sa.Table(
    "edge_heartbeats",
    metadata,
    sa.Column("edge_id", sa.String(length=50), nullable=False),
    sa.Column("site_id", sa.String(length=50), nullable=False),
    sa.Column("schema_version", sa.String(length=20), nullable=False),
    sa.Column("config_revision", sa.String(length=50), nullable=True),
    sa.Column("deployment_revision", sa.String(length=50), nullable=True),
    sa.Column("camera_id", sa.String(length=50), nullable=True),
    sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("overall_status", sa.String(length=30), nullable=False),
    sa.Column("status_reason_code", sa.String(length=50), nullable=True),
    sa.Column("clock_sync_status", sa.String(length=30), nullable=True),
    sa.Column("clock_offset_ms", sa.Float(), nullable=True),
    sa.Column("services_data", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("edge_id"),
)
sa.Index(
    "ix_edge_heartbeats_last_heartbeat_at",
    metadata.tables["edge_heartbeats"].c["last_heartbeat_at"],
    unique=False,
)
sa.Index(
    "ix_edge_heartbeats_site_id", metadata.tables["edge_heartbeats"].c["site_id"], unique=False
)
sa.Index(
    "ix_edge_heartbeats_site_last",
    metadata.tables["edge_heartbeats"].c["site_id"],
    metadata.tables["edge_heartbeats"].c["last_heartbeat_at"],
    unique=False,
)
sa.Table(
    "edge_v2_artifacts",
    metadata,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("site_id", sa.String(length=128), nullable=False),
    sa.Column("edge_id", sa.String(length=128), nullable=False),
    sa.Column("kind", sa.String(length=32), nullable=False),
    sa.Column("key_hash", sa.String(length=64), nullable=False),
    sa.Column("artifact_key", sa.JSON(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("site_id", "edge_id", "kind", "key_hash", name="uq_edge_v2_artifact"),
)
sa.Table(
    "edge_v2_messages",
    metadata,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("site_id", sa.String(length=128), nullable=False),
    sa.Column("edge_id", sa.String(length=128), nullable=False),
    sa.Column("message_id", sa.String(length=128), nullable=False),
    sa.Column("message_type", sa.String(length=32), nullable=False),
    sa.Column("config_revision", sa.String(length=128), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("body_hash", sa.String(length=64), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("instance_id", sa.String(length=128), nullable=True),
    sa.Column("sequence", sa.BigInteger(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("site_id", "edge_id", "message_id", name="uq_edge_v2_message"),
)
sa.Index(
    "ix_edge_v2_latest",
    metadata.tables["edge_v2_messages"].c["site_id"],
    metadata.tables["edge_v2_messages"].c["edge_id"],
    metadata.tables["edge_v2_messages"].c["message_type"],
    metadata.tables["edge_v2_messages"].c["generated_at"],
    unique=False,
)
sa.Index(
    "ix_edge_v2_receipt",
    metadata.tables["edge_v2_messages"].c["site_id"],
    metadata.tables["edge_v2_messages"].c["edge_id"],
    metadata.tables["edge_v2_messages"].c["message_type"],
    metadata.tables["edge_v2_messages"].c["received_at"],
    unique=False,
)
sa.Table(
    "media_clip_jobs",
    metadata,
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("event_id", sa.String(length=128), nullable=False),
    sa.Column("camera_id", sa.String(length=128), nullable=False),
    sa.Column("pit_id", sa.String(length=50), nullable=False),
    sa.Column("category", sa.String(length=20), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("clip_id", sa.String(length=36), nullable=True),
    sa.Column("status", sa.String(length=20), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("error_code", sa.String(length=100), nullable=True),
    sa.Column("snapshot", sa.JSON(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("camera_id", "event_id", name="uq_media_job_event"),
    sa.UniqueConstraint("clip_id"),
)
sa.Index(
    "ix_media_clip_jobs_next_attempt_at",
    metadata.tables["media_clip_jobs"].c["next_attempt_at"],
    unique=False,
)
sa.Index("ix_media_clip_jobs_pit_id", metadata.tables["media_clip_jobs"].c["pit_id"], unique=False)
sa.Table(
    "media_indices",
    metadata,
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("segment_id", sa.String(length=100), nullable=False),
    sa.Column("pit_id", sa.String(length=50), nullable=False),
    sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("file_path", sa.String(length=255), nullable=False),
    sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
    sa.Column("duration_seconds", sa.Float(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Index("ix_media_indices_pit_id", metadata.tables["media_indices"].c["pit_id"], unique=False)
sa.Index(
    "ix_media_indices_pit_start",
    metadata.tables["media_indices"].c["pit_id"],
    metadata.tables["media_indices"].c["start_time"],
    unique=False,
)
sa.Index(
    "ix_media_indices_segment_id", metadata.tables["media_indices"].c["segment_id"], unique=True
)
sa.Index(
    "ix_media_indices_start_time", metadata.tables["media_indices"].c["start_time"], unique=False
)
sa.Table(
    "notification_recipients",
    metadata,
    sa.Column("id", sa.String(length=50), nullable=False),
    sa.Column("version_id", sa.Integer(), nullable=False),
    sa.Column("name", sa.String(length=100), nullable=False),
    sa.Column("team", sa.String(length=100), nullable=False),
    sa.Column("email", sa.String(length=255), nullable=False),
    sa.Column("phone", sa.String(length=50), nullable=True),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("channels", sa.String(length=100), nullable=False),
    sa.Column("subscription_mode", sa.String(length=50), nullable=False),
    sa.Column("event_types", sa.String(length=255), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Index(
    "ix_notification_recipients_email",
    metadata.tables["notification_recipients"].c["email"],
    unique=True,
)
sa.Table(
    "pit_configs",
    metadata,
    sa.Column("id", sa.String(length=50), nullable=False),
    sa.Column("name", sa.String(length=100), nullable=False),
    sa.Column("total_depth_cm", sa.Float(), nullable=False),
    sa.Column("warning_threshold_percent", sa.Float(), nullable=False),
    sa.Column("critical_threshold_percent", sa.Float(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Table(
    "scrap_metrics",
    metadata,
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("measurement_id", sa.String(length=100), nullable=True),
    sa.Column("idempotency_payload_hash", sa.String(length=64), nullable=True),
    sa.Column("site_id", sa.String(length=50), nullable=True),
    sa.Column("edge_id", sa.String(length=50), nullable=True),
    sa.Column("pit_id", sa.String(length=50), nullable=False),
    sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("overall_quality", sa.String(length=20), nullable=False),
    sa.Column("quality_reason_code", sa.String(length=50), nullable=True),
    sa.Column("lidar1_distance_cm", sa.Float(), nullable=True),
    sa.Column("lidar2_distance_cm", sa.Float(), nullable=True),
    sa.Column("calculated_height_cm", sa.Float(), nullable=True),
    sa.Column("fill_ratio_percent", sa.Float(), nullable=True),
    sa.Column("state", sa.String(length=20), nullable=False),
    sa.Column("sensor1_status", sa.String(length=20), nullable=False),
    sa.Column("sensor2_status", sa.String(length=20), nullable=False),
    sa.Column("sensors_data", sa.Text(), nullable=True),
    sa.Column("is_valid", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Index(
    "ix_scrap_metrics_measured_at", metadata.tables["scrap_metrics"].c["measured_at"], unique=False
)
sa.Index(
    "ix_scrap_metrics_measurement_id",
    metadata.tables["scrap_metrics"].c["measurement_id"],
    unique=True,
)
sa.Index("ix_scrap_metrics_pit_id", metadata.tables["scrap_metrics"].c["pit_id"], unique=False)
sa.Index(
    "ix_scrap_metrics_pit_measured",
    metadata.tables["scrap_metrics"].c["pit_id"],
    metadata.tables["scrap_metrics"].c["measured_at"],
    unique=False,
)
sa.Table(
    "alert_policies",
    metadata,
    sa.Column("pit_id", sa.String(length=50), nullable=False),
    sa.Column("pre_alert_enabled", sa.Boolean(), nullable=False),
    sa.Column("send_delay_minutes", sa.Integer(), nullable=False),
    sa.Column("repeat_interval_minutes", sa.Integer(), nullable=False),
    sa.Column("maximum_repeat_count", sa.Integer(), nullable=False),
    sa.Column("recovery_notification_enabled", sa.Boolean(), nullable=False),
    sa.Column(
        "event_types",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    ),
    sa.Column("version_id", sa.Integer(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("maximum_repeat_count >= 1", name="ck_policy_repeat_count"),
    sa.CheckConstraint("repeat_interval_minutes >= 0", name="ck_policy_repeat_interval"),
    sa.CheckConstraint("send_delay_minutes >= 0", name="ck_policy_delay"),
    sa.CheckConstraint("version_id >= 1", name="ck_policy_version"),
    sa.ForeignKeyConstraint(["pit_id"], ["pit_configs.id"]),
    sa.PrimaryKeyConstraint("pit_id"),
)
sa.Table(
    "auth_sessions",
    metadata,
    sa.Column("token_hash", sa.String(length=64), nullable=False),
    sa.Column("username", sa.String(length=64), nullable=False),
    sa.Column("credential_version", sa.Integer(), nullable=False),
    sa.Column("csrf_token", sa.String(length=64), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["username"], ["auth_users.username"]),
    sa.PrimaryKeyConstraint("token_hash"),
)
sa.Index("ix_auth_session_expiry", metadata.tables["auth_sessions"].c["expires_at"], unique=False)
sa.Index("ix_auth_sessions_username", metadata.tables["auth_sessions"].c["username"], unique=False)
sa.Table(
    "edge_v2_alert_links",
    metadata,
    sa.Column("alert_id", sa.Integer(), nullable=False),
    sa.Column("pit_id", sa.String(length=128), nullable=False),
    sa.Column("site_id", sa.String(length=128), nullable=False),
    sa.Column("edge_id", sa.String(length=128), nullable=False),
    sa.Column("assessment_id", sa.String(length=128), nullable=False),
    sa.Column("message_id", sa.String(length=128), nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["alert_id"], ["alert_logs.id"]),
    sa.PrimaryKeyConstraint("alert_id"),
)
sa.Index(
    "ix_v2_alert_source",
    metadata.tables["edge_v2_alert_links"].c["pit_id"],
    metadata.tables["edge_v2_alert_links"].c["site_id"],
    metadata.tables["edge_v2_alert_links"].c["edge_id"],
    metadata.tables["edge_v2_alert_links"].c["alert_id"],
    unique=False,
)
sa.Table(
    "edge_v2_operations",
    metadata,
    sa.Column("pit_id", sa.String(length=128), nullable=False),
    sa.Column("site_id", sa.String(length=128), nullable=False),
    sa.Column("edge_id", sa.String(length=128), nullable=False),
    sa.Column("state", sa.String(length=16), nullable=False),
    sa.Column("message_id", sa.String(length=128), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_alert_id", sa.Integer(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["last_alert_id"], ["alert_logs.id"]),
    sa.PrimaryKeyConstraint("pit_id"),
)
sa.Table(
    "notification_reads",
    metadata,
    sa.Column("username", sa.String(length=64), nullable=False),
    sa.Column("alert_id", sa.Integer(), nullable=False),
    sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["alert_id"], ["alert_logs.id"]),
    sa.ForeignKeyConstraint(["username"], ["auth_users.username"]),
    sa.PrimaryKeyConstraint("username", "alert_id"),
)
sa.Index(
    "ix_notification_reads_alert", metadata.tables["notification_reads"].c["alert_id"], unique=False
)
