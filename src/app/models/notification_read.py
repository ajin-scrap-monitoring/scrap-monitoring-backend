"""Per-account notification acknowledgments without changing the shared alert log."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class NotificationRead(Base):
    __tablename__ = "notification_reads"

    username: Mapped[str] = mapped_column(
        String(64), ForeignKey("auth_users.username"), primary_key=True
    )
    alert_id: Mapped[int] = mapped_column(Integer, ForeignKey("alert_logs.id"), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_notification_reads_alert", "alert_id"),)
