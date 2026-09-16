"""WHEP WebRTC Streaming Session Service."""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from src.app.core.config import get_settings

logger = logging.getLogger(__name__)

STREAM_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")

# Valid mock SDP Answer template for local/fallback testing
MOCK_SDP_ANSWER = (
    "v=0\r\n"
    "o=- 1234567890 2 IN IP4 127.0.0.1\r\n"
    "s=Scrap Monitoring Camera Live Stream\r\n"
    "t=0 0\r\n"
    "a=ice-options:trickle\r\n"
    "a=group:BUNDLE 0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f\r\n"
    "a=sendonly\r\n"
    "a=mid:0\r\n"
)


@dataclass
class WebRTCSession:
    """In-memory state for an active WHEP streaming playback session."""

    session_id: str
    stream_id: str
    etag: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upstream_location: str | None = None
    ice_candidates: list[str] = field(default_factory=list)

    def is_expired(self, ttl_seconds: int) -> bool:
        """Check if session exceeded TTL."""
        expiry = self.last_activity_at + timedelta(seconds=ttl_seconds)
        return datetime.now(UTC) > expiry


class WebRTCSessionService:
    """Manages WHEP WebRTC signaling sessions and forwards to upstream Media Server."""

    def __init__(self) -> None:
        self._sessions: dict[str, WebRTCSession] = {}

    def _purge_expired_sessions(self) -> None:
        """Remove sessions that exceeded TTL."""
        settings = get_settings()
        expired_ids = [
            sid
            for sid, s in self._sessions.items()
            if s.is_expired(settings.WHEP_SESSION_TTL_SECONDS)
        ]
        for sid in expired_ids:
            logger.info("Purging expired WebRTC session: %s", sid)
            self._sessions.pop(sid, None)

    async def create_session(
        self,
        stream_id: str,
        sdp_offer: str,
    ) -> tuple[str, str, str]:
        """Create a WHEP WebRTC session from SDP offer.

        Returns:
            tuple[session_id, etag, sdp_answer]
        """
        self._purge_expired_sessions()
        settings = get_settings()

        if not STREAM_ID_REGEX.match(stream_id):
            raise ValueError(f"Invalid streamId format: {stream_id}")

        if not sdp_offer or not sdp_offer.strip():
            raise ValueError("SDP offer content cannot be empty")

        session_id = str(uuid.uuid4())
        sdp_answer: str | None = None
        upstream_location: str | None = None
        etag: str | None = None

        # 1. Forward to upstream Media Server if configured
        if settings.MEDIA_SERVER_WHEP_URL:
            upstream_url = f"{settings.MEDIA_SERVER_WHEP_URL.rstrip('/')}/{stream_id}/whep"
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.post(
                        upstream_url,
                        content=sdp_offer,
                        headers={"Content-Type": "application/sdp"},
                    )
                    if resp.status_code == 201:
                        sdp_answer = resp.text
                        upstream_location = resp.headers.get("Location")
                        etag = resp.headers.get("ETag")
                    else:
                        logger.warning(
                            "Upstream Media Server returned status %s for stream %s",
                            resp.status_code,
                            stream_id,
                        )
            except Exception as exc:
                logger.warning("Failed to connect to upstream Media Server: %s", exc)

        # 2. Fallback to mock SDP answer if upstream is not available
        if sdp_answer is None:
            if not settings.WHEP_MOCK_FALLBACK:
                msg = (
                    f"Upstream Media Server unavailable and mock fallback disabled for stream: "
                    f"{stream_id}"
                )
                raise RuntimeError(msg)
            sdp_answer = MOCK_SDP_ANSWER
            hash_suffix = hashlib.sha256(f"{session_id}:{stream_id}".encode()).hexdigest()[:16]
            etag = f'"whep-{hash_suffix}"'

        if not etag:
            etag = f'"whep-{hashlib.sha256(sdp_answer.encode()).hexdigest()[:16]}"'

        session = WebRTCSession(
            session_id=session_id,
            stream_id=stream_id,
            etag=etag,
            upstream_location=upstream_location,
        )
        self._sessions[session_id] = session
        logger.info("Created WebRTC WHEP session %s for stream %s", session_id, stream_id)
        return session_id, etag, sdp_answer

    async def update_ice_candidate(
        self,
        session_id: str,
        ice_data: str,
        if_match: str | None = None,
    ) -> bool:
        """Process Trickle ICE candidate or SDP answer patch.

        Returns:
            True on success.
        Raises:
            KeyError: if session does not exist or expired.
            ValueError: if If-Match precondition check fails.
        """
        self._purge_expired_sessions()
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"Session {session_id} not found or expired")

        if if_match and if_match.strip() != session.etag.strip():
            raise ValueError(
                f"ETag mismatch for session {session_id}: expected {session.etag}, got {if_match}"
            )

        session.last_activity_at = datetime.now(UTC)
        session.ice_candidates.append(ice_data)

        # Forward ICE patch to upstream if available
        if session.upstream_location:
            try:
                headers = {"Content-Type": "application/trickle-ice-sdpfrag"}
                if if_match:
                    headers["If-Match"] = if_match
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.patch(
                        session.upstream_location,
                        content=ice_data,
                        headers=headers,
                    )
            except Exception as exc:
                logger.warning("Upstream ICE patch error: %s", exc)

        return True

    async def delete_session(self, session_id: str) -> bool:
        """Terminate a WebRTC playback session and release resources.

        Returns:
            True on success.
        Raises:
            KeyError: if session not found.
        """
        session = self._sessions.pop(session_id, None)
        if not session:
            raise KeyError(f"Session {session_id} not found")

        # Forward DELETE to upstream if available
        if session.upstream_location:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.delete(session.upstream_location)
            except Exception as exc:
                logger.warning("Upstream DELETE session error: %s", exc)

        logger.info("Deleted WebRTC WHEP session: %s", session_id)
        return True

    def get_session(self, session_id: str) -> WebRTCSession | None:
        """Retrieve session by ID."""
        session = self._sessions.get(session_id)
        if session and session.is_expired(get_settings().WHEP_SESSION_TTL_SECONDS):
            self._sessions.pop(session_id, None)
            return None
        return session


# Singleton instance
webrtc_service = WebRTCSessionService()
