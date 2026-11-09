"""Asynchronous send-only Gmail transport; provider acceptance is not inbox receipt."""

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path

import httpx

from src.app.core.config import get_settings

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GmailError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False, retry_after: int | None = None):
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(repr=False)
class GmailCredential:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)


def load_credential(filename: str) -> GmailCredential:
    try:
        path = Path(filename)
        if path.stat().st_mode & 0o077:
            raise ValueError("Credential file is not private")
        data = json.loads(path.read_text())
        if (
            set(data.get("scopes", [])) != {SEND_SCOPE}
            or data.get("token_uri") != TOKEN_URL
            or not all(
                isinstance(data.get(key), str) and data[key]
                for key in ("client_id", "client_secret", "refresh_token")
            )
            or not data["client_id"].endswith(".apps.googleusercontent.com")
        ):
            raise ValueError("Invalid send-only credential")
        return GmailCredential(data["client_id"], data["client_secret"], data["refresh_token"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise GmailError("EMAIL_CREDENTIALS_INVALID") from None


class GmailSender:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._access_token = None
        self._expires_at = 0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    async def _token(self) -> str:
        async with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            settings = get_settings()
            if not settings.GMAIL_CREDENTIALS_FILE:
                raise GmailError("EMAIL_NOT_CONFIGURED")
            credential = await asyncio.to_thread(load_credential, settings.GMAIL_CREDENTIALS_FILE)
            try:
                async with asyncio.timeout(settings.GMAIL_TIMEOUT_SECONDS):
                    response = await self._http().post(
                        TOKEN_URL,
                        data={
                            "client_id": credential.client_id,
                            "client_secret": credential.client_secret,
                            "refresh_token": credential.refresh_token,
                            "grant_type": "refresh_token",
                        },
                        timeout=settings.GMAIL_TIMEOUT_SECONDS,
                    )
            except (httpx.RequestError, TimeoutError):
                raise GmailError("EMAIL_AUTH_UNAVAILABLE", retry_after=60) from None
            if response.status_code != 200:
                raise GmailError("EMAIL_AUTH_FAILED")
            try:
                token = response.json()
                scopes = token.get("scope")
                access = token["access_token"]
                expires = token["expires_in"]
                if (
                    not isinstance(access, str)
                    or not access
                    or any(char.isspace() for char in access)
                    or isinstance(expires, bool)
                    or not isinstance(expires, (int, float))
                    or not 0 < expires <= 86400
                    or token.get("token_type", "Bearer").lower() != "bearer"
                    or (scopes is not None and set(scopes.split()) != {SEND_SCOPE})
                ):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                raise GmailError("EMAIL_AUTH_INVALID_RESPONSE") from None
            self._access_token = access
            self._expires_at = time.monotonic() + max(0, expires - 60)
            return access

    async def send(self, recipient: str, subject: str, body: str, identifier: str) -> str:
        settings = get_settings()
        if not settings.GMAIL_EMAIL_ENABLED or not settings.GMAIL_SENDER_EMAIL:
            raise GmailError("EMAIL_DISABLED")
        if not ADDRESS.fullmatch(recipient) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identifier):
            raise GmailError("EMAIL_INVALID_TARGET")
        try:
            message = EmailMessage()
            message["From"] = Address(
                display_name="Ajin_scrap_Team", addr_spec=settings.GMAIL_SENDER_EMAIL
            )
            message["To"] = recipient
            message["Subject"] = subject
            # A stable ID aids diagnosis; it does not make Gmail POST idempotent.
            message["Message-ID"] = f"<{identifier}@scrap-alerts.invalid>"
            message.set_content(body)
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        except (ValueError, TypeError):
            raise GmailError("EMAIL_INVALID_MESSAGE") from None
        for attempt in range(2):
            access = await self._token()
            try:
                async with asyncio.timeout(settings.GMAIL_TIMEOUT_SECONDS):
                    response = await self._http().post(
                        SEND_URL,
                        headers={"Authorization": f"Bearer {access}"},
                        json={"raw": raw},
                        timeout=settings.GMAIL_TIMEOUT_SECONDS,
                    )
            except (httpx.RequestError, TimeoutError):
                # A timeout can occur after Google accepted the message. Never retry it.
                raise GmailError("EMAIL_DELIVERY_UNKNOWN", uncertain=True) from None
            if response.status_code == 401 and attempt == 0:
                # Explicit authentication rejection was not accepted for delivery.
                self._access_token = None
                continue
            if response.status_code == 429:
                raise GmailError("EMAIL_RATE_LIMITED", retry_after=300)
            if response.status_code >= 500:
                raise GmailError("EMAIL_DELIVERY_UNKNOWN", uncertain=True)
            if response.status_code != 200:
                raise GmailError("EMAIL_REJECTED")
            try:
                result = response.json()["id"]
                if not isinstance(result, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", result):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise GmailError("EMAIL_DELIVERY_UNKNOWN", uncertain=True) from None
            return result
        raise GmailError("EMAIL_AUTH_FAILED")
