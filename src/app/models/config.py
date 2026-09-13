"""Pit Configuration and Threshold Master ORM Model."""

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base, utc_now


class PitConfig(Base):
    """Configuration and threshold master for scrap pits."""

    __tablename__ = "pit_configs"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    total_depth_cm: Mapped[float] = mapped_column(Float, nullable=False, default=300.0)
    warning_threshold_percent: Mapped[float] = mapped_column(Float, nullable=False, default=75.0)
    critical_threshold_percent: Mapped[float] = mapped_column(Float, nullable=False, default=85.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
