"""Operational and Collection Audit Logs ORM Models."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class AlertLog(Base):
    """Log records of threshold alerts triggered by the alert engine."""

    __tablename__ = "alert_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    fill_ratio_percent: Mapped[float] = mapped_column(Float, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="LOG")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SENT")
    message: Mapped[str] = mapped_column(Text, nullable=False)


class CollectionLog(Base):
    """Log records of scrap collection (haul) events and post-collection state."""

    __tablename__ = "collection_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    residual_height_cm: Mapped[float] = mapped_column(Float, nullable=False)
    operator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_collection_logs_pit_completed", "pit_id", "completed_at"),)
