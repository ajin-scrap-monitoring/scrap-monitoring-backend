"""Media Recording Index Metadata ORM Model."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class MediaIndex(Base):
    """Metadata index for recorded media segments (video bytes stored in media service)."""

    __tablename__ = "media_indices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    segment_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    pit_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (Index("ix_media_indices_pit_start", "pit_id", "start_time"),)
