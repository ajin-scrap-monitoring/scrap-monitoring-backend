"""Selected-resource thumbnails: permissions, upstream isolation and fail-closed paths."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from test_daily_recordings import RID, login, recording
from test_media_control import FakeMedia

from src.app.core.config import get_settings
from src.app.services.media_client import MediaServiceError, media_client
from src.app.services.thumbnail_service import thumbnail

CLIP = f"/api/v1/media/clips/{RID}"
DAY = f"/api/v2/recordings/daily/{RID}"


@pytest.fixture
async def thumbnails(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://media.test/api/v1")
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "http://media.test/api/v2")
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", None)
    monkeypatch.setattr(settings, "MEDIA_CAMERA_ID", "camera-main")
    fake = FakeMedia()
    fake.status = "ready"
    day = recording()
    day.update(status="closed", export_state="ready")
    data = {
        "clip": fake.detail(),
        "day": day,
        "requests": [],
        "changes": {},
        "error": None,
        "post_status": 202,
        "raise": None,
    }

    async def handle(request):
        data["requests"].append(request)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.url.host == "media.test" and not request.url.query
        if data["raise"]:
            raise data["raise"]("synthetic failure", request=request)
        daily = request.url.path.startswith("/api/v2")
        if request.url.path.endswith(("/thumbnail-info", "/thumbnail")):
            if data["error"]:
                status, code = data["error"]
                return httpx.Response(status, json={"error": {"code": code, "message": "secret"}})
            out = {
                "recording_id" if daily else "clip_id": RID,
                "camera_id": "camera-main",
                "status": "ready",
                "representative_at": None,
                "time_basis": None,
                "content_path": request.url.path.removesuffix("-info"),
                "width": 320,
                "height": 240,
                "size_bytes": 10621,
                "generated_at": "2026-11-04T15:18:35.042Z",
                "reason_code": "TIMELINE_UNAVAILABLE",
            }
            if request.method == "POST":
                out.update(
                    status="pending",
                    **dict.fromkeys(
                        (
                            "content_path",
                            "width",
                            "height",
                            "size_bytes",
                            "generated_at",
                            "reason_code",
                        )
                    ),
                )
            out.update(data["changes"])
            return httpx.Response(
                data["post_status"] if request.method == "POST" else 200, json=out
            )
        return httpx.Response(200, json=data["day" if daily else "clip"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as mock:
        monkeypatch.setattr(media_client, "_client", mock)
        yield data


@pytest.mark.parametrize("base,resource", [(CLIP, "clips"), (DAY, "daily-recordings")])
async def test_viewer_json_and_image_handoff(client, thumbnails, base, resource):
    info_path = base + "/thumbnail-info"
    image = f"/media/{resource}/{RID}/thumbnail"
    anonymous = await client.get(info_path)
    assert anonymous.status_code == 401
    assert anonymous.headers["cache-control"] == "no-store"
    assert anonymous.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(image)).status_code == 401
    assert thumbnails["requests"] == []
    csrf = await login(client)
    r = await client.get(info_path)
    assert r.status_code == 200, r.text
    assert r.json()["content_path"] == image
    assert r.json()["representative_at"] is None and r.json()["time_basis"] is None
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    for method in (client.get, client.head):
        response = await method(image)
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["x-accel-redirect"].endswith(f"/{resource}/{RID}/thumbnail")
    assert all(r.method == "GET" for r in thumbnails["requests"])
    assert (await client.post(base + "/thumbnail", json={}, headers=csrf)).status_code == 403
    await client.delete("/api/v1/session", headers=csrf)
    assert (await client.get(image)).status_code == 401


@pytest.mark.parametrize("base", [CLIP, DAY])
async def test_explicit_admin_prepare(client, thumbnails, base):
    csrf = await login(client, "admin")
    path = base + "/thumbnail"
    assert (await client.post(path, json={})).status_code == 403
    for body in ({"retry_failed": "true"}, {"extra": True}, {"path": "/etc/passwd"}):
        assert (await client.post(path, json=body, headers=csrf)).status_code == 422
    assert thumbnails["requests"] == []
    response = await client.post(path, json={"retry_failed": True}, headers=csrf)
    assert response.status_code == 202, response.text
    assert response.headers["location"] == base + "/thumbnail-info"
    assert response.json()["status"] == "pending"
    assert thumbnails["requests"][-1].method == "POST"
    assert thumbnails["requests"][-1].content == b'{"retry_failed":true}'
    thumbnails["post_status"] = 200
    assert (await client.post(path, json={}, headers=csrf)).status_code == 200


@pytest.mark.parametrize("base", [CLIP, DAY])
@pytest.mark.parametrize(
    "change",
    [
        {"camera_id": "other-camera"},
        {"content_path": "https://external.invalid/image"},
        {"content_path": "/api/v1/clips/../private/thumbnail"},
        {"width": 641},
        {"height": 361},
        {"size_bytes": 524289},
        {"width": True},
        {"generated_at": "2026-11-04T16:00:00+01:00"},
        {"generated_at": 1},
        {"generated_at": None},
        {"time_basis": "server_received_at"},
        {"reason_code": None},
        {"status": "pending"},
        {"reason_code": "secret-internal-path"},
    ],
)
async def test_reject_bad_upstream_contract(client, thumbnails, base, change):
    await login(client)
    thumbnails["changes"].update(change)
    r = await client.get(base + "/thumbnail-info")
    assert r.status_code == 502, r.text
    assert "x-accel-redirect" not in r.headers
    assert "secret" not in r.text and "external.invalid" not in r.text


@pytest.mark.parametrize("base,key", [(CLIP, "clip_id"), (DAY, "recording_id")])
async def test_identity_parent_and_info(client, thumbnails, base, key):
    await login(client)
    other = "550e8400-e29b-41d4-a716-446655440001"
    thumbnails["changes"][key] = other
    assert (await client.get(base + "/thumbnail-info")).status_code == 502
    thumbnails["changes"].clear()
    parent = thumbnails["clip" if base == CLIP else "day"]
    parent[key] = other
    assert (await client.get(base + "/thumbnail-info")).status_code == 502


@pytest.mark.parametrize("resource,key", [("clips", "clip"), ("daily-recordings", "day")])
@pytest.mark.parametrize("state", ["deleted", "expired", "other-camera", "past-expiry"])
async def test_direct_image_cannot_bypass_parent(client, thumbnails, resource, key, state):
    await login(client)
    parent = thumbnails[key]
    if state == "other-camera":
        parent["camera_id"] = "other-camera"
    elif state == "past-expiry":
        parent["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    else:
        parent["status"] = state
        if key == "clip":
            parent["content_path"] = None
            if state == "deleted":
                parent["deleted_at"] = datetime.now(UTC).isoformat()
    r = await client.get(f"/media/{resource}/{RID}/thumbnail")
    assert r.status_code == (404 if state == "other-camera" else 410), r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert len(thumbnails["requests"]) == 1
    assert "x-accel-redirect" not in r.headers


@pytest.mark.parametrize(
    "state,reason,code",
    [
        ("unsupported", None, 404),
        ("pending", None, 409),
        ("unavailable", "NOT_PREPARED", 404),
        ("failed", "GENERATION_TIMEOUT", 503),
    ],
)
async def test_states_have_no_image(client, thumbnails, state, reason, code):
    await login(client)
    thumbnails["changes"] = dict.fromkeys(
        ("content_path", "width", "height", "size_bytes", "generated_at")
    )
    thumbnails["changes"].update(status=state, reason_code=reason)
    r = await client.get(CLIP + "/thumbnail-info")
    assert r.status_code == 200 and r.json()["status"] == state
    r = await client.get(f"/media/clips/{RID}/thumbnail")
    assert r.status_code == code and "x-accel-redirect" not in r.headers


async def test_legacy_and_transport_failures(client, thumbnails):
    await login(client)
    thumbnails["error"] = (404, "OLD_ROUTE_MISSING")
    assert (await client.get(CLIP + "/thumbnail-info")).json()["status"] == "unsupported"
    for code in ("CLIP_DELETED", "CLIP_EXPIRED"):
        thumbnails["error"] = (410, code)
        assert (await client.get(CLIP + "/thumbnail-info")).status_code == 410
    thumbnails["error"] = (503, "THUMBNAIL_STORAGE_UNAVAILABLE")
    r = await client.get(CLIP + "/thumbnail-info")
    assert r.status_code == 503 and r.json()["error"]["code"] == "THUMBNAIL_STORAGE_UNAVAILABLE"
    thumbnails["raise"] = httpx.ReadTimeout
    assert (await client.get(CLIP + "/thumbnail-info")).status_code == 504


async def test_canonical_ids_and_queries_never_reach_media(client, thumbnails):
    await login(client)
    for path in (
        CLIP.replace(RID, RID.upper()),
        CLIP.replace(RID, "invalid"),
        CLIP.replace(RID, RID.replace("-", "")),
    ):
        assert (await client.get(path + "/thumbnail-info")).status_code == 422
    assert (await client.get(CLIP + "/thumbnail-info?retry_failed=true")).status_code == 422
    assert thumbnails["requests"] == []


async def test_proven_timeline_and_legacy_media_info_unchanged(client, thumbnails):
    await login(client)
    thumbnails["changes"].update(
        representative_at="2026-11-04T15:00:00.000Z",
        time_basis="server_received_at",
        reason_code=None,
    )
    response = await client.get(CLIP + "/thumbnail-info")
    assert response.status_code == 200, response.text
    assert response.json()["representative_at"] is not None
    from src.app.schemas.media_info import RecordingThumbnailInfo

    legacy = {"status": "unsupported", "representative_at": None, "content_path": None}
    assert RecordingThumbnailInfo.model_validate(copy.deepcopy(legacy)).model_dump() == legacy


@pytest.mark.parametrize(
    "state,export",
    [
        ("recording", "not_requested"),
        ("finalizing", "processing"),
        ("closed", "failed"),
    ],
)
async def test_day_image_ready_is_independent_of_archive_state(client, thumbnails, state, export):
    await login(client)
    thumbnails["day"].update(status=state, export_state=export)
    r = await client.get(DAY + "/thumbnail-info")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready" and r.json()["representative_at"] is None
    for method in (client.get, client.head):
        image = await method(f"/media/daily-recordings/{RID}/thumbnail")
        assert image.status_code == 200 and image.headers["x-accel-redirect"]
    assert all(x.method == "GET" for x in thumbnails["requests"])


@pytest.mark.parametrize("state", ["collecting", "processing", "failed"])
async def test_non_ready_clip_cannot_claim_ready_thumbnail(client, thumbnails, state):
    from src.app.schemas.media_service import ClipDetail

    await login(client)
    thumbnails["clip"].update(status=state, content_path=None)
    ClipDetail.model_validate(thumbnails["clip"])
    assert (await client.get(CLIP + "/thumbnail-info")).status_code == 502
    assert len(thumbnails["requests"]) == 2


async def test_recording_busy_is_a_transient_unavailable_image(client, thumbnails):
    data = thumbnails
    await login(client)
    data["day"].update(status="recording", export_state="not_requested")
    data["changes"].update(
        status="unavailable",
        reason_code="RECORDING_BUSY",
        **dict.fromkeys(
            [
                "representative_at",
                "time_basis",
                "content_path",
                "width",
                "height",
                "size_bytes",
                "generated_at",
            ]
        ),
    )
    r = await client.get(DAY + "/thumbnail-info")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unavailable" and r.json()["reason_code"] == "RECORDING_BUSY"
    assert (await client.get(f"/media/daily-recordings/{RID}/thumbnail")).status_code == 404
    data["changes"].clear()
    assert (await client.get(DAY + "/thumbnail-info")).json()["status"] == "ready"
    assert all(x.method == "GET" for x in data["requests"])


async def test_preparation_status_and_bounded_timeout(client, thumbnails, monkeypatch):
    csrf = await login(client, "admin")
    thumbnails["changes"]["status"] = "unavailable"
    thumbnails["changes"]["reason_code"] = "NOT_PREPARED"
    assert (await client.post(CLIP + "/thumbnail", json={}, headers=csrf)).status_code == 502
    thumbnails["changes"].clear()

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(media_client, "_request", slow)
    monkeypatch.setattr(get_settings(), "MEDIA_REQUEST_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(MediaServiceError) as caught:
        await thumbnail(UUID(RID))
    assert caught.value.status_code == 504
