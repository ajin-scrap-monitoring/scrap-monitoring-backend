"""Bounded current observations and minute summaries, separate from durable receipts."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class EdgeV2Latest(Base):
    __tablename__ = "edge_v2_latest"

    site_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    edge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class EdgeV2Filter(Base):
    __tablename__ = "edge_v2_filters"

    site_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    edge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class EdgeV2Minute(Base):
    __tablename__ = "edge_v2_minutes"

    site_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    edge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class EdgeV2Transition(Base):
    """Small state/error events remain after periodic observation bodies are replaced."""

    __tablename__ = "edge_v2_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    edge_id: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (Index("ix_v2_transition_source", "site_id", "edge_id", "occurred_at"),)
