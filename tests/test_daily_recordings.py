"""Daily camera JSON contract, browser permissions and calendar/gap semantics."""

import copy

import httpx
import pytest
from pydantic import ValidationError

from src.app.core.config import Settings, get_settings
from src.app.services.media_client import media_client

RID = "550e8400-e29b-41d4-a716-446655440000"
BASE = "/api/v2/recordings/daily"
START = "2026-10-13T15:00:00.000Z"
END = "2026-10-14T15:00:00.000Z"


def recording():
    return {
        "recording_id": RID,
        "camera_id": "camera-main",
        "recording_date": "2026-10-14",
        "timezone": "Asia/Seoul",
        "window_start_at": START,
        "window_end_at": END,
        "status": "recording",
        "coverage": None,
        "has_gaps": True,
        "coverage_as_of_at": "2026-10-13T15:01:00.000Z",
        "playable_until_at": "2026-10-13T15:01:00.000Z",
        "recorded_seconds": 50,
        "gaps": [
            {
                "start_at": "2026-10-13T15:00:20.000Z",
                "end_at": "2026-10-13T15:00:30.000Z",
                "reason_code": "CAMERA_DISCONNECTED",
            }
        ],
        "available_intervals": [
            {
                "start_at": START,
                "end_at": "2026-10-13T15:00:20.000Z",
                "playback_start_seconds": 0,
                "playback_end_seconds": 20,
            },
            {
                "start_at": "2026-10-13T15:00:30.000Z",
                "end_at": "2026-10-13T15:01:00.000Z",
                "playback_start_seconds": 20,
                "playback_end_seconds": 50,
            },
        ],
        "playback_state": "ready",
        "playback_protocol": "hls",
        "playback_path": f"/api/v2/daily-recordings/{RID}/hls/index.m3u8",
        "export_allowed": False,
        "export_state": "not_requested",
        "download_path": None,
        "size_bytes": None,
        "expires_at": None,
        "export_expires_at": None,
        "error": None,
        "revision": 10,
    }


class Camera:
    def __init__(self):
        self.item = recording()
        self.requests = []
        self.error = None
        self.policy = {"enabled": True, "retention_days": 30}

    async def handle(self, request):
        self.requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.url.host == "daily.test"
        assert request.url.path.startswith("/api/v2/")
        if self.error:
            code, name = self.error
            return httpx.Response(code, json={"error": {"code": name, "message": "private-key"}})
        if request.url.path.endswith("daily-recording-policy"):
            if request.method == "PUT":
                import json

                self.policy = json.loads(request.content)
            return httpx.Response(200, json=self.policy)
        if request.method == "DELETE":
            self.item.update(status="deleted", playback_path=None, download_path=None)
            return httpx.Response(204)
        if request.method == "POST":
            assert not request.content
            if not self.item["export_allowed"]:
                return httpx.Response(409, json={"error": {"code": "DAY_NOT_CLOSED"}})
            ready = self.item["export_state"] == "ready"
            if not ready:
                self.item["export_state"] = "processing"
            return httpx.Response(200 if ready else 202, json=self.item)
        if request.url.path.endswith("/daily-recordings"):
            return httpx.Response(200, json={"items": [self.item], "next_cursor": "opaque+/="})
        return httpx.Response(200, json=self.item)


@pytest.fixture
async def camera(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "http://daily.test/api/v2")
    fake = Camera()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        monkeypatch.setattr(media_client, "_client", client)
        yield fake


async def login(client, username="operator"):
    response = await client.post(
        "/api/v1/session",
        json={
            "username": username,
            "password": "operator1234" if username == "operator" else "admin1234",
        },
    )
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrfToken"]}


async def test_daily_calendar_gaps_and_gateway_paths(client, camera):
    assert (await client.get(BASE)).status_code == 401
    await login(client)
    response = await client.get(
        BASE,
        params={
            "from_date": "2026-10-01",
            "to_date": "2026-10-15",
            "limit": 7,
            "cursor": "opaque+/=",
            "status": "recording",
        },
    )
    assert response.status_code == 200
    page = response.json()
    assert page["next_cursor"] == "opaque+/="
    assert camera.requests[-1].url.params["to_date"] == "2026-10-15"
    assert camera.requests[-1].url.params["limit"] == "7"
    assert camera.requests[-1].url.params["cursor"] == "opaque+/="
    item = page["items"][0]
    assert item["recording_date"] == "2026-10-14"
    assert item["window_start_at"] == "2026-10-13T15:00:00Z"
    assert item["coverage"] is None and item["has_gaps"]
    assert item["recorded_seconds"] == 50
    assert item["available_intervals"][1]["playback_start_seconds"] == 20
    assert len(item["gaps"]) == 1  # Future hours are not invented as missing video.
    assert item["playback_path"] == f"/media/daily-recordings/{RID}/hls/index.m3u8"
    assert item["download_path"] is None
    assert (await client.get(f"{BASE}/{RID}")).json() == item
    # API routes never fetch a playlist or a video body.
    assert (await client.get(f"{BASE}/{RID}/content")).status_code == 404


async def test_viewer_export_csrf_and_partial_day_lifecycle(client, camera):
    headers = await login(client)
    path = f"{BASE}/{RID}/exports"
    assert (await client.post(path)).status_code == 403
    assert (await client.post(path, headers={"X-CSRF-Token": "wrong"})).status_code == 403
    assert (await client.post(path, headers=headers)).status_code == 409
    camera.item.update(status="closed", coverage="partial", export_allowed=True)
    response = await client.post(path, headers=headers)
    assert response.status_code == 202
    assert response.headers["location"] == f"{BASE}/{RID}"
    assert response.json()["export_state"] == "processing"
    assert response.json()["download_path"] is None
    # Polling remains separate from the request; retries reuse the same date ID.
    assert (await client.post(path, headers=headers)).status_code == 202
    camera.item.update(
        export_state="ready",
        download_path=f"/api/v2/daily-recordings/{RID}/content",
        size_bytes=1024,
        export_expires_at=END,
    )
    response = await client.post(path, headers=headers)
    assert response.status_code == 200
    assert (
        response.json()["download_path"] == f"/media/daily-recordings/{RID}/content?download=true"
    )
    assert response.json()["coverage"] == "partial"


async def test_daily_admin_policy_and_delete_permissions(client, camera):
    headers = await login(client)
    assert (await client.get(BASE + "/policy")).status_code == 200
    assert (
        await client.put(BASE + "/policy", headers=headers, json=camera.policy)
    ).status_code == 403
    assert (await client.delete(f"{BASE}/{RID}", headers=headers)).status_code == 403
    headers = await login(client, "admin")
    assert (await client.put(BASE + "/policy", json=camera.policy)).status_code == 403
    policy = {"enabled": False, "retention_days": 7}
    assert (await client.put(BASE + "/policy", headers=headers, json=policy)).json() == policy
    for bad in (
        {**policy, "enabled": 1},
        {**policy, "retention_days": True},
        {**policy, "retention_days": 366},
        {**policy, "extra": 1},
    ):
        assert (await client.put(BASE + "/policy", headers=headers, json=bad)).status_code == 422
    camera.item.update(status="closed", coverage="partial", export_allowed=True)
    assert (await client.delete(f"{BASE}/{RID}", headers=headers)).status_code == 204
    assert (await client.delete(f"{BASE}/{RID}", headers=headers)).status_code == 204
    assert (await client.get(f"{BASE}/{RID}")).json()["playback_path"] is None


@pytest.mark.parametrize(
    "query",
    [
        {"from_date": "2026-10-15", "to_date": "2026-10-15"},
        {"from_date": "2026-10-16", "to_date": "2026-10-15"},
        {"from_date": "invalid"},
        {"from_date": "2026-10-14T00:00:00"},
        {"from_date": "1791936000"},
        {"limit": 101},
        {"limit": 0},
        {"status": "ready"},
    ],
)
async def test_invalid_daily_filters(client, camera, query):
    await login(client)
    assert (await client.get(BASE, params=query)).status_code == 422
    assert not camera.requests


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "INVALID_CURSOR"),
        (404, "DAILY_RECORDING_NOT_FOUND"),
        (409, "NO_RECORDING_DATA"),
        (409, "DAY_FINALIZING"),
        (410, "DAILY_RECORDING_EXPIRED"),
        (410, "DAILY_RECORDING_DELETED"),
        (503, "INSUFFICIENT_STORAGE"),
        (503, "EXPORT_CAPACITY_EXCEEDED"),
    ],
)
async def test_daily_safe_upstream_errors(client, camera, status, code):
    await login(client)
    camera.error = (status, code)
    response = await client.get(f"{BASE}/{RID}")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "private-key" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        {"playback_path": "https://untrusted.test/video"},
        {"download_path": "/api/v2/daily-recordings/other/content"},
        {"window_start_at": "2026-10-14T00:00:00Z"},
        {"coverage_as_of_at": "2026-10-15T00:00:00Z"},
        {"camera_id": "other-camera"},
        {"recording_id": "550e8400-e29b-41d4-a716-446655440001", "playback_path": None},
    ],
)
async def test_daily_rejects_wrong_identity_paths_and_calendar(client, camera, change):
    await login(client)
    camera.item.update(change)
    assert (await client.get(f"{BASE}/{RID}")).status_code == 502


async def test_daily_failures_no_data_and_export_error_are_distinct(client, camera):
    await login(client)
    camera.item.update(
        status="closed",
        coverage="none",
        has_gaps=True,
        recorded_seconds=0,
        available_intervals=[],
        playback_state="unavailable",
        playback_path=None,
        playable_until_at=None,
        export_allowed=False,
    )
    assert (await client.get(f"{BASE}/{RID}")).json()["coverage"] == "none"
    camera.item.update(
        coverage="partial",
        export_state="failed",
        error={"code": "JOB_INTERRUPTED", "message": "private-key"},
    )
    response = await client.get(f"{BASE}/{RID}")
    assert response.json()["status"] == "closed"
    assert response.json()["export_state"] == "failed"
    assert "private-key" not in response.text


async def test_daily_unconfigured_and_timeouts(client, camera, monkeypatch):
    await login(client)
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", None)
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", None)
    assert (await client.get(BASE)).status_code == 503
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://daily.test/api/v1")
    assert (await client.get(BASE)).status_code == 200  # Derived v2 URL, no token sent.

    async def timeout(request):
        raise httpx.ReadTimeout("private-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        response = await client.get(BASE)
        assert response.status_code == 504 and "private-key" not in response.text


def test_daily_endpoint_configuration():
    Settings(_env_file=None, MEDIA_DAILY_SERVICE_URL="http://media.test/api/v2")
    for url in (
        "http://media.test/api/v1",
        "http://user:secret@media.test/api/v2",
        "http://media.test/api/v2?token=secret",
        "http://media.test:0/api/v2",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, MEDIA_DAILY_SERVICE_URL=url)


async def test_reject_gaps_past_confirmed_time(client, camera):
    await login(client)
    gap = copy.deepcopy(camera.item["gaps"][0])
    gap["end_at"] = END
    camera.item["gaps"] = [gap]
    assert (await client.get(f"{BASE}/{RID}")).status_code == 502


async def test_equal_capture_timestamps_keep_playback_mapping(client, camera):
    await login(client)
    interval = camera.item["available_intervals"][0]
    interval["end_at"] = interval["start_at"]
    gap = camera.item["gaps"][0]
    gap["end_at"] = gap["start_at"]
    response = await client.get(f"{BASE}/{RID}")
    assert response.status_code == 200
    actual = response.json()["available_intervals"][0]
    assert actual["start_at"] == actual["end_at"]
    assert actual["playback_end_seconds"] == 20
    interval["end_at"] = "2026-10-13T14:59:59Z"
    assert (await client.get(f"{BASE}/{RID}")).status_code == 502
