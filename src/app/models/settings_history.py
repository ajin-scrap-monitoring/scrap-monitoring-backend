"""Append-only application history for successful administrator setting changes."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base, utc_now


class SettingsHistory(Base):
    __tablename__ = "settings_history"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # Snapshots deliberately have no user/pit FK: later account changes must not erase history.
    actor_username: Mapped[str] = mapped_column(String(64))
    actor_display_name: Mapped[str] = mapped_column(String(100))
    pit_id: Mapped[str] = mapped_column(String(50))
    resource: Mapped[str] = mapped_column(String(20))
    operation: Mapped[str] = mapped_column(String(10))
    before: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    after: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    changed_fields: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))

    __table_args__ = (
        CheckConstraint(
            "resource IN ('alert_settings', 'pit_config')", name="ck_settings_history_resource"
        ),
        CheckConstraint("operation IN ('create', 'update')", name="ck_settings_history_operation"),
        Index("ix_settings_history_pit_id", "pit_id", "id"),
        Index("ix_settings_history_actor_id", "actor_username", "id"),
        Index("ix_settings_history_time", "occurred_at"),
    )
