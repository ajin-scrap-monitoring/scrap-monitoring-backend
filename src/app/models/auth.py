"""Persistent accounts and revocable browser sessions; raw cookie tokens are never stored."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.db.base import Base


class AuthUser(Base):
    __tablename__ = "auth_users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    credential_version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (CheckConstraint("role IN ('administrator', 'viewer')", name="ck_auth_role"),)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("auth_users.username"), index=True)
    credential_version: Mapped[int] = mapped_column(Integer)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_auth_session_expiry", "expires_at"),)
