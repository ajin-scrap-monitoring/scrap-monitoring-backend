"""Send-only authorization, bounded requests and ambiguous delivery outcomes."""

import asyncio
import base64
import json
from email import message_from_bytes

import httpx
import pytest

from src.app.core.config import get_settings
from src.app.services.gmail_sender import SEND_SCOPE, SEND_URL, TOKEN_URL, GmailError, GmailSender


@pytest.fixture
def gmail_settings(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "synthetic.apps.googleusercontent.com",
                "client_secret": "synthetic-secret",
                "refresh_token": "synthetic-refresh",
                "token_uri": TOKEN_URL,
                "scopes": [SEND_SCOPE],
            }
        )
    )
    path.chmod(0o600)
    settings = get_settings()
    for name, value in {
        "GMAIL_EMAIL_ENABLED": True,
        "GMAIL_SENDER_EMAIL": "sender@example.test",
        "GMAIL_CREDENTIALS_FILE": str(path),
        "GMAIL_TIMEOUT_SECONDS": 0.1,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return path


def token_response():
    return httpx.Response(
        200,
        json={
            "access_token": "synthetic-access",
            "expires_in": 3600,
            "scope": SEND_SCOPE,
            "token_type": "Bearer",
        },
    )


async def test_only_send_endpoint_and_refresh_credentials_are_used(gmail_settings):
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == TOKEN_URL:
            assert b"synthetic-refresh" in request.content
            assert b"gmail.readonly" not in request.content
            return token_response()
        assert str(request.url) == SEND_URL
        assert request.headers["Authorization"] == "Bearer synthetic-access"
        raw = json.loads(request.content)["raw"]
        message = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert message["To"] == "receiver@example.test"
        assert message["From"] == "sender@example.test"
        return httpx.Response(200, json={"id": "synthetic-message"})

    sender = GmailSender(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        assert (
            await sender.send("receiver@example.test", "합성 알림", "내용", "synthetic-id")
            == "synthetic-message"
        )
        await sender.send("receiver@example.test", "합성 알림", "내용", "synthetic-other")
        assert [str(r.url) for r in requests] == [TOKEN_URL, SEND_URL, SEND_URL]
    finally:
        await sender.close()


@pytest.mark.parametrize("change", ["scope", "permissions", "uri", "missing", "not_object"])
async def test_invalid_credentials_fail_before_network(gmail_settings, change):
    data = json.loads(gmail_settings.read_text())
    if change == "scope":
        data["scopes"].append("https://www.googleapis.com/auth/gmail.readonly")
    elif change == "permissions":
        gmail_settings.chmod(0o644)
    elif change == "uri":
        data["token_uri"] = "https://untrusted.example.test/token"
    elif change == "missing":
        data.pop("refresh_token")
    else:
        data = []
    gmail_settings.write_text(json.dumps(data))

    def unexpected(request):
        raise AssertionError("No network request should occur")

    sender = GmailSender(httpx.AsyncClient(transport=httpx.MockTransport(unexpected)))
    try:
        with pytest.raises(GmailError, match="^EMAIL_CREDENTIALS_INVALID$"):
            await sender.send("receiver@example.test", "title", "body", "synthetic-id")
    finally:
        await sender.close()


@pytest.mark.parametrize(
    "result,uncertain,code",
    [
        ("timeout", True, "EMAIL_DELIVERY_UNKNOWN"),
        ("drip", True, "EMAIL_DELIVERY_UNKNOWN"),
        (500, True, "EMAIL_DELIVERY_UNKNOWN"),
        (429, False, "EMAIL_RATE_LIMITED"),
        (403, False, "EMAIL_REJECTED"),
        ("bad_success", True, "EMAIL_DELIVERY_UNKNOWN"),
    ],
)
async def test_delivery_failures_do_not_resend(gmail_settings, result, uncertain, code):
    deliveries = []

    async def handle(request):
        if str(request.url) == TOKEN_URL:
            return token_response()
        deliveries.append(request)
        if result == "timeout":
            raise httpx.ReadTimeout("synthetic-secret")
        if result == "drip":
            await asyncio.sleep(1)
        if result == "bad_success":
            return httpx.Response(200, json={"unexpected": "synthetic-secret"})
        return httpx.Response(result, json={"error": "synthetic-secret"})

    sender = GmailSender(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(GmailError) as error:
            await sender.send("receiver@example.test", "title", "body", "synthetic-id")
        assert error.value.code == code
        assert error.value.uncertain is uncertain
        assert "synthetic-secret" not in str(error.value)
        assert len(deliveries) == 1
    finally:
        await sender.close()


async def test_explicit_401_refreshes_once_without_retrying_uncertain_request(gmail_settings):
    counts = {"tokens": 0, "sends": 0}

    def handle(request):
        if str(request.url) == TOKEN_URL:
            counts["tokens"] += 1
            return token_response()
        counts["sends"] += 1
        return (
            httpx.Response(401)
            if counts["sends"] == 1
            else httpx.Response(200, json={"id": "accepted"})
        )

    sender = GmailSender(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        assert (
            await sender.send("receiver@example.test", "title", "body", "synthetic-id")
            == "accepted"
        )
        assert counts == {"tokens": 2, "sends": 2}
    finally:
        await sender.close()


async def test_refresh_rejects_added_scope_without_sending(gmail_settings):
    def handle(request):
        assert str(request.url) == TOKEN_URL
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic",
                "expires_in": 3600,
                "scope": SEND_SCOPE + " https://www.googleapis.com/auth/gmail.readonly",
            },
        )

    sender = GmailSender(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(GmailError, match="EMAIL_AUTH_INVALID_RESPONSE"):
            await sender.send("receiver@example.test", "title", "body", "synthetic-id")
    finally:
        await sender.close()


async def test_caller_cancellation_propagates_without_retry(gmail_settings):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def handle(request):
        if str(request.url) == TOKEN_URL:
            return token_response()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    sender = GmailSender(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    task = asyncio.create_task(
        sender.send("receiver@example.test", "title", "body", "synthetic-id")
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
    finally:
        await sender.close()
