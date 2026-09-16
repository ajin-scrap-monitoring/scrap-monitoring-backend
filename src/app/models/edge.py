"""Edge Platform Heartbeat ORM Model."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class EdgeHeartbeatModel(Base):
    """Latest device status snapshot reported by Edge Platform orchestrator."""

    __tablename__ = "edge_heartbeats"

    edge_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0")
    config_revision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    deployment_revision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    camera_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    overall_status: Mapped[str] = mapped_column(String(30), nullable=False, default="HEALTHY")
    status_reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    clock_sync_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    clock_offset_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    services_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_edge_heartbeats_site_last", "site_id", "last_heartbeat_at"),)
