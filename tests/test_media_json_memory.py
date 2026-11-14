"""Bound control JSON without reading large media metadata into the application."""

import asyncio
import gzip

import httpx
import pytest

from src.app.core.config import get_settings
from src.app.services import media_client as module
from src.app.services.media_client import MediaClient, MediaServiceError


@pytest.fixture(autouse=True)
def configure_media(monkeypatch):
    monkeypatch.setattr(get_settings(), "MEDIA_DAILY_SERVICE_URL", "http://media.test/api/v2")


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("declared", [True, False])
async def test_large_response_stops_early_and_next_request_succeeds(monkeypatch, declared):
    monkeypatch.setattr(module, "MEDIA_JSON_MAX_BYTES", 128)
    monkeypatch.setattr(module, "MEDIA_JSON_CHUNK_BYTES", 32)
    stream = Stream([b"x" * 32] * 1000)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, stream=stream, headers={"Content-Length": "32000"} if declared else {}
            )
        return httpx.Response(200, json={"ok": True})

    client = MediaClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(MediaServiceError) as error:
            await client._request("GET", "daily-recordings/example", (200,), daily=True)
        assert (error.value.status_code, error.value.code) == (502, "MEDIA_BAD_RESPONSE")
        assert stream.closed and stream.reads <= 5
        result = await client._request("GET", "daily-recordings/example", (200,), daily=True)
        assert result.json() == {"ok": True}
    finally:
        await client.close()


@pytest.mark.parametrize("compressed", [True, False])
async def test_limit_applies_to_decoded_body_and_exact_limit_is_allowed(monkeypatch, compressed):
    monkeypatch.setattr(module, "MEDIA_JSON_MAX_BYTES", 64)
    monkeypatch.setattr(module, "MEDIA_JSON_CHUNK_BYTES", 16)
    payload = b'{"message":"' + b"a" * 50 + b'"}'
    assert len(payload) == 64
    stream = Stream([gzip.compress(payload) if compressed else payload])
    client = MediaClient()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=stream, headers={"Content-Encoding": "gzip"} if compressed else {}
            )
        )
    )
    try:
        response = await client._request("GET", "daily-recordings/example", (200,), daily=True)
        assert response.json() == {"message": "a" * 50}
        assert stream.closed
    finally:
        await client.close()


async def test_compressed_response_cannot_bypass_limit(monkeypatch):
    monkeypatch.setattr(module, "MEDIA_JSON_MAX_BYTES", 128)
    monkeypatch.setattr(module, "MEDIA_JSON_CHUNK_BYTES", 32)
    stream = Stream([gzip.compress(b"a" * 4096)])
    client = MediaClient()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"})
        )
    )
    try:
        with pytest.raises(MediaServiceError, match="MEDIA_BAD_RESPONSE"):
            await client._request("GET", "daily-recordings/example", (200,), daily=True)
        assert stream.closed
    finally:
        await client.close()


async def test_stream_remains_inside_wall_clock_timeout(monkeypatch):
    monkeypatch.setattr(module, "MEDIA_JSON_CHUNK_BYTES", 16)
    monkeypatch.setattr(get_settings(), "MEDIA_REQUEST_TIMEOUT_SECONDS", 0.02)
    stream = Stream([b"x" * 16] * 20, delay=0.01)
    client = MediaClient()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    )
    try:
        with pytest.raises(MediaServiceError, match="MEDIA_TIMEOUT"):
            await client._request("GET", "daily-recordings/example", (200,), daily=True)
        assert stream.closed and stream.reads < 20
    finally:
        await client.close()
