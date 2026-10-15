"""Persistent per-pit notification policy; thresholds remain in PitConfig."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base, utc_now


class AlertPolicy(Base):
    __tablename__ = "alert_policies"

    pit_id: Mapped[str] = mapped_column(String(50), ForeignKey("pit_configs.id"), primary_key=True)
    pre_alert_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    send_delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    repeat_interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    maximum_repeat_count: Mapped[int] = mapped_column(Integer, default=3)
    recovery_notification_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    event_types: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        default=lambda: ["collection_required", "measurement_error", "device_error"],
    )
    version_id: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        CheckConstraint("send_delay_minutes >= 0", name="ck_policy_delay"),
        CheckConstraint("repeat_interval_minutes >= 0", name="ck_policy_repeat_interval"),
        CheckConstraint("maximum_repeat_count >= 1", name="ck_policy_repeat_count"),
        CheckConstraint("version_id >= 1", name="ck_policy_version"),
    )
