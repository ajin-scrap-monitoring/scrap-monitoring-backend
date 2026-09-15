"""Notification Recipient ORM Model."""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class NotificationRecipientModel(Base):
    """Registered alert notification recipient."""

    __tablename__ = "notification_recipients"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    version_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    team: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    channels: Mapped[str] = mapped_column(String(100), default="email,sms", nullable=False)
    subscription_mode: Mapped[str] = mapped_column(String(50), default="global", nullable=False)
    event_types: Mapped[str] = mapped_column(
        String(255),
        default="collection_required,measurement_error,device_error",
        nullable=False,
    )
