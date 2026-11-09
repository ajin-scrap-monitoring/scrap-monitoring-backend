"""Whole-request deadlines with actual HTTP reads and reusable connections."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from src.app.core.config import get_settings
from src.app.services.media_client import MediaClient, MediaServiceError


class SyntheticResult(BaseModel):
    ok: bool


@asynccontextmanager
async def slow_json_server():
    """Each slow read is timely, while the complete body exceeds the budget."""
    requests = []
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split(b" ")[1]
            requests.append(path)
            body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            parts = (b"{", b'"ok"', b":", b"true", b"}") if path == b"/slow" else (body,)
            for part in parts:
                if path == b"/slow":
                    await asyncio.sleep(0.06)
                writer.write(part)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        await server.wait_closed()
        remaining = list(handlers)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.fixture
async def media(monkeypatch):
    client = MediaClient()
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", SecretStr("synthetic-token"))
    try:
        yield client, settings
    finally:
        await client.close()


@pytest.mark.parametrize("override", [False, True])
async def test_drip_response_deadline_releases_connection_without_retry(
    media, monkeypatch, override
):
    client, settings = media
    monkeypatch.setattr(settings, "MEDIA_REQUEST_TIMEOUT_SECONDS", 1.0 if override else 0.18)
    client._client = httpx.AsyncClient(
        timeout=settings.MEDIA_REQUEST_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=1),
    )
    async with slow_json_server() as (url, requests):
        monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", url)
        with pytest.raises(MediaServiceError) as error:
            await client._request(
                "GET", "/slow", (200,), timeout_seconds=0.18 if override else None
            )
        assert (error.value.status_code, error.value.code, error.value.retryable) == (
            504,
            "MEDIA_TIMEOUT",
            True,
        )
        assert str(error.value) == "MEDIA_TIMEOUT"
        response = await client._request("GET", "/fast", (200,))
        assert response.json() == {"ok": True}
        assert requests == [b"/slow", b"/fast"]


@pytest.mark.parametrize("metadata", [False, True])
async def test_longer_explicit_budget_preserves_metadata_reads(media, monkeypatch, metadata):
    client, settings = media
    monkeypatch.setattr(settings, "MEDIA_REQUEST_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("src.app.services.media_client.MEDIA_INFO_TIMEOUT_SECONDS", 0.6)
    async with slow_json_server() as (url, requests):
        monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", url)
        if metadata:
            result = await client.metadata("/slow", SyntheticResult)
            assert result.ok is True
        else:
            response = await client._request("GET", "/slow", (200,), timeout_seconds=0.6)
            assert response.json() == {"ok": True}
        assert requests == [b"/slow"]


async def test_caller_cancellation_is_preserved_and_next_request_succeeds(media, monkeypatch):
    client, settings = media
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://media.test")
    monkeypatch.setattr(settings, "MEDIA_REQUEST_TIMEOUT_SECONDS", 10.0)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def handle(request):
        if request.url.path == "/slow":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return httpx.Response(200, json={"ok": True})

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    task = asyncio.create_task(client._request("GET", "/slow", (200,)))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert (await client._request("GET", "/fast", (200,))).json() == {"ok": True}
