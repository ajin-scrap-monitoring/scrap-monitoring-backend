"""Authenticated read-only storage scope, unknown values and safe failures."""

import copy

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_daily_recordings import login

from src.app.core.config import get_settings
from src.app.models.media_job import MediaClipJob
from src.app.services.media_client import media_client

PATH = "/api/v1/media/storage/status"
OBSERVATIONS = (
    "observation_started_at",
    "observed_at",
    "filesystem_total_bytes",
    "filesystem_available_bytes",
    "daily_usage_bytes",
    "daily_reserved_bytes",
)
READY = {
    "status": "ready",
    "storage_type": "filesystem",
    "observation_started_at": "2026-01-01T00:00:00Z",
    "observed_at": "2026-01-01T00:00:01Z",
    "filesystem_total_bytes": 10000,
    "filesystem_available_bytes": 3000,
    "media_limit_bytes": 8000,
    "daily_limit_bytes": 7000,
    "daily_usage_bytes": 6000,
    "daily_reserved_bytes": 100,
    "reason_code": None,
}


@pytest.fixture
async def storage(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://storage.test/api/v1")
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "http://other.test/api/v2")
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", SecretStr("synthetic-unused-token"))
    data = {"http": 200, "body": copy.deepcopy(READY), "requests": [], "error": None}

    def handle(request):
        data["requests"].append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://storage.test/api/v1/storage/status"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if data["error"]:
            raise data["error"]
        return httpx.Response(data["http"], json=data["body"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(media_client, "_client", client)
        yield data


@pytest.mark.parametrize("role", ["operator", "admin"])
async def test_private_read_preserves_scope_and_creates_no_jobs(
    client, test_db_session, storage, role
):
    assert (await client.get(PATH)).status_code == 401
    assert storage["requests"] == []
    await login(client, role)
    before = await test_db_session.scalar(select(func.count()).select_from(MediaClipJob))
    storage["body"]["internal_path"] = "/private/storage"
    for _ in range(2):
        response = await client.get(PATH)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == READY
    assert len(storage["requests"]) == 2
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == before
    assert "/private" not in response.text


async def test_zero_usage_unknown_limits_and_unknown_reservation_are_distinct(client, storage):
    await login(client)
    storage["body"].update(
        filesystem_total_bytes=0,
        filesystem_available_bytes=0,
        daily_usage_bytes=0,
        daily_reserved_bytes=0,
        media_limit_bytes=None,
        daily_limit_bytes=None,
    )
    response = await client.get(PATH)
    assert response.status_code == 200
    assert response.json()["daily_reserved_bytes"] == 0
    assert response.json()["daily_limit_bytes"] is None
    storage["body"].update(daily_reserved_bytes=None, reason_code="RESERVATION_SCOPE_UNKNOWN")
    response = await client.get(PATH)
    assert response.status_code == 200
    assert response.json()["daily_usage_bytes"] == 0
    assert response.json()["daily_reserved_bytes"] is None
    assert response.json()["reason_code"] == "RESERVATION_SCOPE_UNKNOWN"


@pytest.mark.parametrize("reason", ["STORAGE_UNAVAILABLE", "STORAGE_SNAPSHOT_BUSY"])
async def test_unavailable_keeps_known_limits_without_fake_zero(client, storage, reason):
    await login(client)
    storage["http"] = 503
    storage["body"].update(status="unavailable", reason_code=reason)
    storage["body"].update(dict.fromkeys(OBSERVATIONS))
    response = await client.get(PATH)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert all(response.json()[field] is None for field in OBSERVATIONS)
    assert response.json()["media_limit_bytes"] == READY["media_limit_bytes"]
    assert response.json()["daily_limit_bytes"] == READY["daily_limit_bytes"]


@pytest.mark.parametrize(
    "change",
    [
        {"filesystem_total_bytes": True},
        {"daily_usage_bytes": "100"},
        {"daily_usage_bytes": -1},
        {"daily_reserved_bytes": 0.5},
        {"filesystem_available_bytes": 10001},
        {"filesystem_total_bytes": None},
        {"daily_usage_bytes": None},
        {"observed_at": None},
        {"observed_at": "2025-12-31T23:59:59Z"},
        {"observed_at": "2026-01-01T09:00:01+09:00"},
        {"observed_at": "2026-01-01T00:00:01"},
        {"observed_at": 1767225601},
        {"observed_at": "1767225601"},
        {"media_limit_bytes": -1},
        {"daily_limit_bytes": False},
        {"daily_reserved_bytes": None},
        {"reason_code": "RESERVATION_SCOPE_UNKNOWN"},
        {"reason_code": "STORAGE_UNAVAILABLE"},
        {"reason_code": "/private/error"},
        {"storage_type": "sd_card"},
    ],
)
async def test_invalid_observation_has_safe_error(client, storage, change):
    await login(client)
    storage["body"].update(change)
    response = await client.get(PATH)
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "MEDIA_BAD_RESPONSE"
    assert "/private" not in response.text


@pytest.mark.parametrize("http,unavailable", [(200, True), (503, False)])
async def test_http_status_must_agree_with_observation(client, storage, http, unavailable):
    await login(client)
    storage["http"] = http
    if unavailable:
        storage["body"].update(status="unavailable", reason_code="STORAGE_UNAVAILABLE")
        storage["body"].update(dict.fromkeys(OBSERVATIONS))
    assert (await client.get(PATH)).status_code == 502


async def test_unavailable_cannot_keep_old_success_values(client, storage):
    await login(client)
    storage["http"] = 503
    storage["body"].update(status="unavailable", reason_code="STORAGE_UNAVAILABLE")
    assert (await client.get(PATH)).status_code == 502


@pytest.mark.parametrize(
    "error,status,code",
    [
        (httpx.ReadTimeout("private-upstream"), 504, "MEDIA_TIMEOUT"),
        (httpx.ConnectError("private-upstream"), 503, "MEDIA_UNAVAILABLE"),
    ],
)
async def test_transport_failure_is_not_successful_empty_storage(
    client, storage, error, status, code
):
    await login(client)
    storage["error"] = error
    response = await client.get(PATH)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "private-upstream" not in response.text


async def test_missing_source_and_unsupported_query_do_not_call_media(client, storage, monkeypatch):
    await login(client)
    assert (await client.get(PATH, params={"path": "/private/storage"})).status_code == 422
    monkeypatch.setattr(get_settings(), "MEDIA_SERVICE_URL", None)
    response = await client.get(PATH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MEDIA_NOT_CONFIGURED"
    assert storage["requests"] == []


async def test_openapi_describes_both_unavailable_shapes(client):
    schema = (await client.get("/openapi.json")).json()
    response = schema["paths"][PATH]["get"]["responses"]["503"]
    variants = response["content"]["application/json"]["schema"]["anyOf"]
    assert {v["$ref"].rsplit("/", 1)[-1] for v in variants} == {
        "MediaStorageStatus",
        "HTTPErrorResponse",
        "MediaErrorResponse",
    }
