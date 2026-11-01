"""Durable event-to-clip outbox, independent of legacy recording indices."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class MediaClipJob(Base):
    """Persist before contacting media so lost responses reuse the same event ID."""

    __tablename__ = "media_clip_jobs"
    __table_args__ = (UniqueConstraint("camera_id", "event_id", name="uq_media_job_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    camera_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clip_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class EventExportAlias(Base):
    """Retain each user's request identity when it reuses a canonical export job."""

    __tablename__ = "event_export_aliases"

    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("media_clip_jobs.id"), nullable=False, index=True
    )
