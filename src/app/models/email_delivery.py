"""Durable mail episodes and delivery receipts; no sender credentials are stored."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class EmailEpisode(Base):
    __tablename__ = "email_episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(255), nullable=False)
    condition_token: Mapped[str] = mapped_column(String(64), nullable=False)
    condition_state: Mapped[str] = mapped_column(String(50), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(50), nullable=False)
    address_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    component_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    component_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_email_episodes_pit_phase", "pit_id", "phase"),
        CheckConstraint("sent_count >= 0", name="ck_email_episode_count"),
    )


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    episode_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("email_episodes.id"), nullable=True
    )
    recipient_id: Mapped[str] = mapped_column(String(50), nullable=False)
    address_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("episode_id", "phase", "sequence", name="uq_email_episode_delivery"),
        Index("ix_email_deliveries_due", "status", "due_at"),
        Index("ix_email_deliveries_attempt", "attempted_at"),
        CheckConstraint("attempts >= 0 AND sequence >= 1", name="ck_email_delivery_count"),
    )
