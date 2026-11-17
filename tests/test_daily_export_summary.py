"""Export checks never load full history or replace POST with a summary GET."""

from contextlib import asynccontextmanager

import httpx
import pytest
from test_daily_recordings import BASE, RID, login
from test_daily_recordings import camera as camera

from src.app.main import app
from src.app.services.media_client import media_client


@asynccontextmanager
async def export_upstream(monkeypatch, fake, *, state="ready", status=None, error=None, bad=None):
    requests = []

    async def handle(request):
        requests.append(request)
        assert not request.url.query
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if request.method == "GET":
            assert request.url.path == f"/api/v2/daily-recordings/{RID}/summary"
            return await fake.handle(request)
        assert request.method == "POST"
        assert request.url.path == f"/api/v2/daily-recordings/{RID}/exports/summary"
        assert request.content == b""
        if error:
            return httpx.Response(error[0], json={"error": {"code": error[1], "message": "secret"}})
        item = {k: v for k, v in fake.item.items() if k not in ("gaps", "available_intervals")}
        item.update(export_state=state)
        if state == "ready":
            item.update(download_path=f"/api/v2/daily-recordings/{RID}/content", size_bytes=1024)
        if state == "failed":
            item.update(error={"code": "RECORDING_STORAGE_ERROR", "message": "secret"})
        item.update(bad or {})
        return httpx.Response(status or (200 if state == "ready" else 202), json=item)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        yield requests


@pytest.mark.parametrize("state", ["ready", "processing", "failed", "not_requested"])
async def test_export_preserves_state_without_full_history(client, camera, monkeypatch, state):
    headers = await login(client)
    camera.item.update(status="closed", coverage="partial", export_allowed=True)
    # An accidental full-detail request is rejected by the transport, regardless of size.
    async with export_upstream(monkeypatch, camera, state=state) as requests:
        response = await client.post(f"{BASE}/{RID}/exports", headers=headers)
    assert response.status_code == (200 if state == "ready" else 202)
    assert [r.method for r in requests] == ["GET", "POST"]
    assert response.headers["location"] == f"{BASE}/{RID}/summary"
    assert response.headers["cache-control"] == "no-store"
    item = response.json()
    assert item["export_state"] == state and item["coverage"] == "partial" and item["has_gaps"]
    assert (
        not {"gaps", "available_intervals", "gap_count", "available_interval_count"} & item.keys()
    )
    assert len(response.content) < 8192
    if state == "ready":
        assert item["download_path"] == f"/media/daily-recordings/{RID}/content?download=true"
    if state == "failed":
        assert item["error"]["code"] == "RECORDING_STORAGE_ERROR"
        assert "secret" not in response.text


@pytest.mark.parametrize(
    "error",
    [
        (400, "INVALID_ARGUMENT"),
        (404, "DAILY_RECORDING_NOT_FOUND"),
        (409, "DAY_NOT_CLOSED"),
        (409, "DAY_FINALIZING"),
        (409, "NO_RECORDING_DATA"),
        (410, "DAILY_RECORDING_DELETED"),
        (410, "DAILY_RECORDING_EXPIRED"),
        (503, "RECORDING_STORAGE_ERROR"),
        (503, "CATALOG_SUMMARY_UNAVAILABLE"),
    ],
)
async def test_post_errors_are_not_hidden_by_successful_get(client, camera, monkeypatch, error):
    headers = await login(client)
    async with export_upstream(monkeypatch, camera, error=error) as requests:
        response = await client.post(f"{BASE}/{RID}/exports", headers=headers)
    assert response.status_code == error[0]
    assert response.json()["error"]["code"] == error[1]
    assert "secret" not in response.text
    assert [r.method for r in requests] == ["GET", "POST"]


async def test_export_ownership_and_csrf_stop_before_upstream_post(client, camera, monkeypatch):
    async with export_upstream(monkeypatch, camera) as requests:
        assert (await client.post(f"{BASE}/{RID}/exports")).status_code == 401
        headers = await login(client)
        assert (await client.post(f"{BASE}/{RID}/exports")).status_code == 403
        assert not requests
        camera.item["camera_id"] = "another-camera"
        response = await client.post(f"{BASE}/{RID}/exports", headers=headers)
        assert response.status_code == 502
        assert [r.method for r in requests] == ["GET"]


@pytest.mark.parametrize(
    "state,status,bad",
    [
        ("ready", 202, {}),
        ("failed", 200, {}),
        ("ready", 200, {"recording_id": "550e8400-e29b-41d4-a716-446655440001"}),
        ("ready", 200, {"camera_id": "another-camera"}),
        ("ready", 200, {"download_path": "https://example.invalid/private"}),
        ("ready", 200, {"gaps": []}),
    ],
)
async def test_invalid_export_response_is_rejected(client, camera, monkeypatch, state, status, bad):
    headers = await login(client)
    async with export_upstream(monkeypatch, camera, state=state, status=status, bad=bad):
        response = await client.post(f"{BASE}/{RID}/exports", headers=headers)
    assert response.status_code == 502


def test_export_schema_matches_lightweight_success_responses():
    responses = app.openapi()["paths"][f"{BASE}/{{recording_id}}/exports"]["post"]["responses"]
    for status in ("200", "202"):
        assert responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/DailyRecordingMetadata"
        }
