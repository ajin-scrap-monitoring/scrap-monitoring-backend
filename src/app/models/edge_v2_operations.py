"""Persistent v2 hysteresis/cooldown state and links to dashboard notifications."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class EdgeV2Operation(Base):
    __tablename__ = "edge_v2_operations"

    pit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    edge_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), default="NORMAL")
    message_id: Mapped[str] = mapped_column(String(128))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    critical_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alert_id: Mapped[int | None] = mapped_column(ForeignKey("alert_logs.id"), nullable=True)


class EdgeV2AlertLink(Base):
    __tablename__ = "edge_v2_alert_links"

    alert_id: Mapped[int] = mapped_column(ForeignKey("alert_logs.id"), primary_key=True)
    pit_id: Mapped[str] = mapped_column(String(128))
    site_id: Mapped[str] = mapped_column(String(128))
    edge_id: Mapped[str] = mapped_column(String(128))
    assessment_id: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_v2_alert_source", "pit_id", "site_id", "edge_id", "alert_id"),)
