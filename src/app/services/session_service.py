"""Session management and authentication service."""

import hmac
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from src.app.schemas.session import Session, User, UserRole

logger = logging.getLogger(__name__)

# Default credential configuration (can be overridden via environment variables)
DEFAULT_USERS: dict[str, dict[str, str]] = {
    "admin": {
        "password": os.environ.get("ADMIN_PASSWORD", "admin1234"),
        "displayName": "관리자",
        "role": "administrator",
    },
    "operator": {
        "password": os.environ.get("OPERATOR_PASSWORD", "operator1234"),
        "displayName": "현장 운영자",
        "role": "viewer",
    },
}

SESSION_COOKIE_NAME = "scrap_session"


class SessionData:
    """Internal session storage container."""

    def __init__(self, user: User, expires_at: datetime, csrf_token: str):
        self.user = user
        self.expires_at = expires_at
        self.csrf_token = csrf_token

    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    def to_schema(self) -> Session:
        return Session(
            user=self.user,
            expiresAt=self.expires_at,
            csrfToken=self.csrf_token,
        )


class SessionService:
    """Manages user authentication and in-memory session lifecycles."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionData] = {}

    def authenticate(self, username: str, password: str) -> User | None:
        """Verify user credentials and return user profile if matched."""
        user_entry = DEFAULT_USERS.get(username)
        if not user_entry:
            return None

        expected_password = user_entry["password"]
        if not hmac.compare_digest(password, expected_password):
            return None

        role: UserRole = "administrator" if user_entry["role"] == "administrator" else "viewer"
        return User(
            id=username,
            displayName=user_entry["displayName"],
            role=role,
        )

    def create_session(self, user: User, persistent: bool = False) -> tuple[str, Session]:
        """Create a new session with CSRF token and return (session_id, session)."""
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_hex(16)

        lifetime = timedelta(days=30) if persistent else timedelta(hours=24)
        expires_at = datetime.now(UTC) + lifetime

        session_data = SessionData(user=user, expires_at=expires_at, csrf_token=csrf_token)
        self._sessions[session_id] = session_data

        logger.info(
            "Created session for user '%s' (role: %s, persistent: %s)",
            user.id,
            user.role,
            persistent,
        )
        return session_id, session_data.to_schema()

    def get_session(self, session_id: str) -> Session | None:
        """Retrieve active session by session_id, cleaning up expired sessions."""
        session_data = self._sessions.get(session_id)
        if not session_data:
            return None

        if session_data.is_expired():
            del self._sessions[session_id]
            logger.info("Removed expired session: %s...", session_id[:8])
            return None

        return session_data.to_schema()

    def delete_session(self, session_id: str) -> bool:
        """Delete an existing session."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.info("Deleted session: %s...", session_id[:8])
            return True
        return False

    def clear_all(self) -> None:
        """Clear all sessions (mainly for test isolation)."""
        self._sessions.clear()


session_service = SessionService()
