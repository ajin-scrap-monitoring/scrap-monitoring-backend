"""Time-series Scrap Metric ORM Model."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class ScrapMetric(Base):
    """Time-series metrics from LiDAR and derived scrap loading states."""

    __tablename__ = "scrap_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    measurement_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True, index=True
    )
    idempotency_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    edge_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True, default="pit-01")
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Measurement quality: GOOD, DEGRADED, INVALID
    overall_quality: Mapped[str] = mapped_column(String(20), nullable=False, default="GOOD")
    quality_reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # LiDAR raw summary measurements (cm)
    lidar1_distance_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    lidar2_distance_cm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Derived scrap level metrics (nullable for INVALID quality measurements)
    calculated_height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_ratio_percent: Mapped[float | None] = mapped_column(Float, nullable=True)

    # State: NORMAL, WARNING, CRITICAL, ERROR
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="NORMAL")

    # Sensor diagnostics: OK, WARN, ERROR, DISCONNECTED
    sensor1_status: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")
    sensor2_status: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")

    # Full sensor list JSON from edge
    sensors_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Overall validity flag
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_scrap_metrics_pit_measured", "pit_id", "measured_at"),)
