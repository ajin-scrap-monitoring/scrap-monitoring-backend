"""Time-series Scrap Metric ORM Model."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class ScrapMetric(Base):
    """Time-series metrics from LiDAR and derived scrap loading states."""

    __tablename__ = "scrap_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True, default="pit-01")
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # LiDAR raw summary measurements (cm)
    lidar1_distance_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    lidar2_distance_cm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Derived scrap level metrics
    calculated_height_cm: Mapped[float] = mapped_column(Float, nullable=False)
    fill_ratio_percent: Mapped[float] = mapped_column(Float, nullable=False)

    # State: NORMAL, WARNING, CRITICAL
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="NORMAL")

    # Sensor diagnostics: OK, WARN, ERROR, DISCONNECTED
    sensor1_status: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")
    sensor2_status: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")

    # Overall validity flag
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_scrap_metrics_pit_measured", "pit_id", "measured_at"),)
