"""DB-backed session management and authentication service."""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.passwords import hash_password, validate_new_password, verify_password
from src.app.models.auth import AuthSession, AuthUser
from src.app.schemas.session import Session, User, UserRole

SESSION_COOKIE_NAME = "scrap_session"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def user_schema(row: AuthUser) -> User:
    return User(id=row.username, displayName=row.display_name, role=cast(UserRole, row.role))


async def commit_auth(db: AsyncSession) -> None:
    try:
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise


@dataclass(frozen=True)
class AuthenticatedUser:
    user: User
    credential_version: int
    password_hash: str = field(repr=False)


class SessionService:
    """Manages user authentication and persistent session lifecycles."""

    async def authenticate(
        self, db: AsyncSession, username: str, password: str
    ) -> AuthenticatedUser | None:
        """Verify user credentials and return a versioned user profile if matched."""
        row = await db.get(AuthUser, username, populate_existing=True)
        encoded = row.password_hash if row else None
        user = user_schema(row) if row else None
        version = row.credential_version if row else None
        active = row.is_active if row else False
        await db.rollback()
        verified = await verify_password(encoded, password)
        if not verified or not active or user is None or version is None or encoded is None:
            return None
        return AuthenticatedUser(user, version, encoded)

    async def create_session(
        self, db: AsyncSession, user: AuthenticatedUser, persistent: bool = False
    ) -> tuple[str, Session]:
        """Create a new session with CSRF token and return (session_id, session)."""
        row = await db.scalar(
            select(AuthUser)
            .where(
                AuthUser.username == user.user.id,
                AuthUser.is_active.is_(True),
                AuthUser.credential_version == user.credential_version,
                AuthUser.password_hash == user.password_hash,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(401, "Invalid username or password")
        now = datetime.now(UTC)
        lifetime = timedelta(days=30) if persistent else timedelta(hours=24)
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_hex(16)
        # Max-age: 30 days for persistent, 24 hours for normal. DB expiry is authoritative.
        expires_at = now + lifetime
        record = AuthSession(
            token_hash=token_digest(session_id),
            username=row.username,
            credential_version=row.credential_version,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
        schema = Session(user=user_schema(row), expiresAt=expires_at, csrfToken=csrf_token)
        # Bound expired session storage during ordinary login activity.
        await db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        db.add(record)
        await commit_auth(db)
        return session_id, schema

    async def get_session(self, db: AsyncSession, session_id: str) -> Session | None:
        """Retrieve an active session and recheck the account's current role and activation."""
        if not 1 <= len(session_id) <= 128:
            return None
        result = (
            await db.execute(
                select(AuthSession, AuthUser)
                .join(AuthUser, AuthUser.username == AuthSession.username)
                .where(
                    AuthSession.token_hash == token_digest(session_id),
                    AuthSession.expires_at > datetime.now(UTC),
                    AuthUser.is_active.is_(True),
                    AuthSession.credential_version == AuthUser.credential_version,
                )
                .execution_options(populate_existing=True)
            )
        ).first()
        if result is None:
            return None
        session, user = result
        return Session(
            user=user_schema(user), expiresAt=utc(session.expires_at), csrfToken=session.csrf_token
        )

    async def delete_session(self, db: AsyncSession, session_id: str) -> None:
        """Delete an existing session so every process observes logout."""
        await db.execute(
            delete(AuthSession).where(AuthSession.token_hash == token_digest(session_id))
        )
        await commit_auth(db)

    async def reset_password(self, db: AsyncSession, username: str, password: str) -> bool:
        """Administrative password reset invalidates every previously issued session."""
        validate_new_password(password)
        encoded = await hash_password(password)
        row = await db.scalar(
            select(AuthUser).where(AuthUser.username == username).with_for_update()
        )
        if row is None:
            await db.rollback()
            return False
        await db.execute(
            update(AuthUser)
            .where(AuthUser.username == username)
            .values(password_hash=encoded, credential_version=AuthUser.credential_version + 1)
        )
        await db.execute(delete(AuthSession).where(AuthSession.username == username))
        await commit_auth(db)
        return True


session_service = SessionService()
