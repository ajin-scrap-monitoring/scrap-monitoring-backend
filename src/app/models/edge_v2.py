"""Durable v2 message receipts and immutable versioned artifacts."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class EdgeV2Message(Base):
    __tablename__ = "edge_v2_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    edge_id: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str] = mapped_column(String(128))
    message_type: Mapped[str] = mapped_column(String(32))
    config_revision: Mapped[str] = mapped_column(String(128))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("site_id", "edge_id", "message_id", name="uq_edge_v2_message"),
        Index("ix_edge_v2_latest", "site_id", "edge_id", "message_type", "generated_at"),
        Index("ix_edge_v2_receipt", "site_id", "edge_id", "message_type", "received_at"),
    )


class EdgeV2Artifact(Base):
    """One immutable config, calibration, scan source, processed scan or assessment revision.

    Keys and payloads preserve the wire identifiers. A digest only keeps the composite
    SQL index bounded; full keys are compared too before accepting a repeated artifact.
    """

    __tablename__ = "edge_v2_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    edge_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    key_hash: Mapped[str] = mapped_column(String(64))
    artifact_key: Mapped[list[Any]] = mapped_column(JSON)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("site_id", "edge_id", "kind", "key_hash", name="uq_edge_v2_artifact"),
    )
