"""Parent-only operations work when the legacy full recording is too large."""

from contextlib import asynccontextmanager

import httpx
from test_daily_recordings import BASE, RID, login
from test_daily_recordings import camera as camera
from test_daily_thumbnail_links import calendar_thumbnails as calendar_thumbnails
from test_media_info import metadata as metadata
from test_thumbnails import thumbnails as thumbnails

from src.app.services.media_client import media_client


@asynccontextmanager
async def bounded_upstream(monkeypatch):
    original = media_client._client
    requests = []
    full_paths = {
        f"/api/v2/daily-recordings/{RID}",
        "/api/v2/cameras/camera-main/daily-recordings",
    }

    async def handle(request):
        requests.append(request)
        if request.method == "GET" and request.url.path in full_paths:
            return httpx.Response(503, json={"error": {"code": "DAILY_RESPONSE_TOO_LARGE"}})
        return await original._transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        yield requests
    assert not any(r.method == "GET" and r.url.path in full_paths for r in requests)


async def test_selected_thumbnail_uses_only_metadata(client, thumbnails, monkeypatch):
    await login(client)
    async with bounded_upstream(monkeypatch) as requests:
        response = await client.get(f"{BASE}/{RID}/thumbnail-info")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["content_path"] == f"/media/daily-recordings/{RID}/thumbnail"
        assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["summary", "thumbnail-info"]
        assert all(r.method == "GET" for r in requests)


async def test_calendar_thumbnail_uses_summary_list_and_parent(
    client, calendar_thumbnails, monkeypatch
):
    await login(client)
    async with bounded_upstream(monkeypatch) as requests:
        response = await client.get(BASE + "/by-date/2026-10-14/thumbnail-info")
        assert response.status_code == 200
        assert len(requests) == 3 and all(r.method == "GET" for r in requests)
        assert requests[0].url.path.endswith("/daily-recordings/summary")
        assert requests[1].url.path.endswith(f"/{RID}/summary")


async def test_file_metadata_does_not_load_recording_arrays(client, metadata, monkeypatch):
    await login(client)
    async with bounded_upstream(monkeypatch) as requests:
        response = await client.get(f"{BASE}/{RID}/media-info")
        assert response.status_code == 200 and response.json()["recording_id"] == RID
        assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["summary", "media-info"]


async def test_delete_still_checks_admin_csrf_and_ownership_without_full_history(
    client, camera, monkeypatch
):
    viewer = await login(client)
    async with bounded_upstream(monkeypatch) as requests:
        assert (await client.delete(f"{BASE}/{RID}", headers=viewer)).status_code == 403
        assert not requests
        admin = await login(client, "admin")
        assert (await client.delete(f"{BASE}/{RID}")).status_code == 403
        assert not requests
        camera.item["camera_id"] = "other-camera"
        assert (await client.delete(f"{BASE}/{RID}", headers=admin)).status_code == 502
        assert all(r.method == "GET" for r in requests)
        camera.item["camera_id"] = "camera-main"
        requests.clear()
        assert (await client.delete(f"{BASE}/{RID}", headers=admin)).status_code == 204
        assert [r.method for r in requests] == ["GET", "DELETE"]
        assert requests[0].url.path.endswith("/summary")
