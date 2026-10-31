"""Actual file information, measured scope and JSON-only authenticated reads."""

import asyncio
import copy

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_daily_recordings import RID, login, recording
from test_media_control import FakeMedia

from src.app.core.config import get_settings
from src.app.models.media_job import MediaClipJob
from src.app.services.media_client import media_client

CLIP = f"/api/v1/media/clips/{RID}/media-info"
DAY = f"/api/v2/recordings/daily/{RID}/media-info"
FORMAT = {
    "codec": "h264",
    "width": 1920,
    "height": 1080,
    "fps": 30000 / 1001,
    "fps_num": 30000,
    "fps_den": 1001,
}
TIMESTAMP = {
    "time_basis": "server_received_at",
    "capture_timestamp_supported": False,
    "capture_timestamp": None,
    "capture_clock_source": None,
    "clock_synchronization": "unavailable",
}
THUMBNAIL = {"status": "unsupported", "representative_at": None, "content_path": None}


def measured():
    return {"status": "ready", "source": "file_probe", **FORMAT, "size_bytes": 123456}


@pytest.fixture
async def metadata(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://media.test/api/v1")
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "http://daily.test/api/v2")
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", SecretStr("synthetic-media-token"))
    monkeypatch.setattr(settings, "MEDIA_CAMERA_ID", "camera-main")
    monkeypatch.setattr(settings, "MEDIA_PIT_ID", "pit-01")
    fake = FakeMedia()
    fake.status = "ready"
    data = {
        "clip_detail": fake.detail(),
        "day_detail": recording(),
        "requests": [],
        "clip": {
            "clip_id": RID,
            "size_bytes": 123456,
            "media_info": measured(),
            "timestamp_info": copy.deepcopy(TIMESTAMP),
            "thumbnail": copy.deepcopy(THUMBNAIL),
        },
        "day": {
            "recording_id": RID,
            "file": None,
            "segments": {
                "status": "partial",
                "total": 10,
                "measured": 1,
                "mixed": None,
                "formats": [{**FORMAT, "segment_count": 1}],
            },
            "timestamp_info": copy.deepcopy(TIMESTAMP),
            "thumbnail": copy.deepcopy(THUMBNAIL),
        },
        "error": None,
        "delay": 0,
    }

    async def handle(request):
        data["requests"].append(request)
        assert request.method == "GET" and "cookie" not in request.headers
        daily = request.url.host == "daily.test"
        assert (
            ("authorization" not in request.headers)
            if daily
            else request.headers["authorization"] == "Bearer synthetic-media-token"
        )
        if request.url.path.endswith("/media-info"):
            if data["delay"]:
                await asyncio.sleep(data["delay"])
            if data["error"]:
                status, code = data["error"]
                return httpx.Response(
                    status, json={"error": {"code": code, "message": "private-secret"}}
                )
            return httpx.Response(200, json=data["day" if daily else "clip"])
        if request.url.path.endswith("/daily-recordings"):
            return httpx.Response(200, json={"items": [data["day_detail"]], "next_cursor": None})
        return httpx.Response(200, json=data["day_detail" if daily else "clip_detail"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as mock:
        monkeypatch.setattr(media_client, "_client", mock)
        yield data


@pytest.mark.parametrize("path", [CLIP, DAY])
async def test_authenticated_json_only_read_and_no_business_writes(
    client, test_db_session, metadata, path
):
    assert (await client.get(path)).status_code == 401
    await login(client, "operator")
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    out = response.json()
    assert out["timestamp_info"] == TIMESTAMP and out["thumbnail"] == THUMBNAIL
    if path == CLIP:
        assert out["media_info"]["fps"] == 30000 / 1001
        assert out["media_info"]["fps_num"] == 30000 and out["media_info"]["fps_den"] == 1001
        assert out["media_info"]["width"] == 1920 and out["size_bytes"] == 123456
    else:
        assert out["file"] is None and out["segments"]["mixed"] is None
        assert out["segments"]["measured"] == 1 and out["segments"]["total"] == 10
    assert len(metadata["requests"]) == 2
    assert metadata["requests"][-1].extensions["timeout"]["read"] == 18.0
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize(
    "reason",
    [
        "file_unavailable",
        "probe_failed",
        "probe_timeout",
        "probe_unavailable",
        "file_changed",
        "metadata_unavailable",
    ],
)
async def test_unknown_preserves_null_values_and_measured_size(client, metadata, reason):
    await login(client)
    info = metadata["clip"]["media_info"]
    info.update(status="unknown", reason_code=reason)
    for key in FORMAT:
        info[key] = None
    response = await client.get(CLIP)
    assert response.status_code == 200, response.text
    out = response.json()["media_info"]
    assert out["status"] == "unknown" and out["reason_code"] == reason
    assert all(out[key] is None for key in FORMAT)
    assert out["size_bytes"] == 123456


@pytest.mark.parametrize(
    "total,measured_count,formats,status,mixed",
    [
        (0, 0, [], "unknown", None),
        (10, 0, [], "unknown", None),
        (10, 2, [{**FORMAT, "segment_count": 2}], "partial", None),
        (2, 2, [{**FORMAT, "segment_count": 2}], "ready", False),
        (
            5,
            2,
            [{**FORMAT, "segment_count": 1}, {**FORMAT, "width": 1280, "segment_count": 1}],
            "partial",
            True,
        ),
        (
            2,
            2,
            [{**FORMAT, "segment_count": 1}, {**FORMAT, "width": 1280, "segment_count": 1}],
            "ready",
            True,
        ),
    ],
)
async def test_segment_measured_scope(
    client, metadata, total, measured_count, formats, status, mixed
):
    await login(client)
    metadata["day"]["segments"] = {
        "total": total,
        "measured": measured_count,
        "formats": formats,
        "status": status,
        "mixed": mixed,
    }
    metadata["day"]["file"] = measured()
    r = await client.get(DAY)
    assert r.status_code == 200, r.text
    assert r.json()["segments"]["mixed"] is mixed
    assert r.json()["file"]["source"] == "file_probe"


@pytest.mark.parametrize(
    "change",
    [
        {"fps": 0},
        {"fps": "NaN"},
        {"fps": 29.97},
        {"fps_den": 0},
        {"fps_num": 10**400},
        {"width": True},
        {"height": 0},
        {"codec": "/private/source/file.mp4"},
        {"status": "unknown", "reason_code": "probe_failed"},
        {"status": "ready", "reason_code": "probe_failed"},
    ],
)
async def test_invalid_file_measurement_fails_safely(client, metadata, change):
    await login(client)
    metadata["clip"]["media_info"].update(change)
    r = await client.get(CLIP)
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "MEDIA_BAD_RESPONSE"
    assert "/private" not in r.text


@pytest.mark.parametrize(
    "change",
    [
        {"mixed": False},
        {"status": "ready"},
        {"measured": 11},
        {"total": True},
        {"formats": []},
        {"formats": [{**FORMAT, "fps_num": 0, "segment_count": 1}]},
    ],
)
async def test_inconsistent_segment_summary_is_not_presented_as_complete(client, metadata, change):
    await login(client)
    metadata["day"]["segments"].update(change)
    assert (await client.get(DAY)).status_code == 502


@pytest.mark.parametrize(
    "path,key,id_key", [(CLIP, "clip", "clip_id"), (DAY, "day", "recording_id")]
)
async def test_identity_mismatch(client, metadata, path, key, id_key):
    await login(client)
    metadata[key][id_key] = "00000000-0000-0000-0000-000000000001"
    r = await client.get(path)
    assert r.status_code == 502 and r.json()["error"]["code"] == "MEDIA_IDENTITY_MISMATCH"


@pytest.mark.parametrize("path,key", [(CLIP, "clip_detail"), (DAY, "day_detail")])
async def test_camera_binding_checked_before_probe(client, metadata, path, key):
    await login(client)
    metadata[key]["camera_id"] = "other-camera"
    assert (await client.get(path)).status_code in (404, 502)
    assert len(metadata["requests"]) == 1


@pytest.mark.parametrize("state,status", [("processing", 409), ("deleted", 410), ("expired", 410)])
async def test_unavailable_clip_does_not_probe(client, metadata, state, status):
    await login(client)
    metadata["clip_detail"].update(status=state, content_path=None)
    assert (await client.get(CLIP)).status_code == status
    assert len(metadata["requests"]) == 1


@pytest.mark.parametrize("state", ["deleted", "expired"])
async def test_deleted_day_does_not_probe(client, metadata, state):
    await login(client)
    metadata["day_detail"].update(status=state, playback_path=None, download_path=None)
    assert (await client.get(DAY)).status_code == 410
    assert len(metadata["requests"]) == 1


@pytest.mark.parametrize(
    "status,code,public_status",
    [
        (503, "METADATA_CAPACITY_EXCEEDED", 503),
        (401, "INTERNAL_ERROR", 502),
        (404, "CLIP_NOT_FOUND", 404),
        (409, "CLIP_NOT_READY", 409),
        (410, "CLIP_DELETED", 410),
    ],
)
async def test_metadata_error_codes_do_not_leak_messages(
    client, metadata, status, code, public_status
):
    await login(client)
    metadata["error"] = (status, code)
    r = await client.get(CLIP)
    assert r.status_code == public_status and "private-secret" not in r.text
    assert len(metadata["requests"]) == 2


async def test_metadata_wall_clock_budget_does_not_retry(client, metadata, monkeypatch):
    await login(client)
    monkeypatch.setattr("src.app.services.media_client.MEDIA_INFO_TIMEOUT_SECONDS", 0.01)
    metadata["delay"] = 0.1
    r = await client.get(CLIP)
    assert r.status_code == 504 and r.json()["error"]["code"] == "MEDIA_TIMEOUT"
    assert len(metadata["requests"]) == 2


async def test_no_probe_on_existing_list_and_detail_reads(client, metadata):
    await login(client)
    for path in [
        "/api/v2/recordings/daily",
        f"/api/v2/recordings/daily/{RID}",
        f"/api/v1/media/clips/{RID}",
    ]:
        response = await client.get(path)
        assert response.status_code == 200, response.text
    assert len(metadata["requests"]) == 3
    assert not any(r.url.path.endswith("/media-info") for r in metadata["requests"])


async def test_extra_private_fields_are_discarded_and_unsupported_claims_rejected(client, metadata):
    await login(client)
    metadata["clip"]["internal_path"] = "/private/source/file.mp4"
    metadata["clip"]["media_info"]["internal_path"] = "/private/source/file.mp4"
    r = await client.get(CLIP)
    assert r.status_code == 200 and "/private" not in r.text
    metadata["clip"]["thumbnail"]["content_path"] = "https://private.test/image.jpg"
    assert (await client.get(CLIP)).status_code == 502
    metadata["clip"]["thumbnail"] = copy.deepcopy(THUMBNAIL)
    metadata["clip"]["timestamp_info"]["capture_timestamp_supported"] = True
    assert (await client.get(CLIP)).status_code == 502


async def test_size_disagreement_and_invalid_uuid(client, metadata):
    await login(client)
    metadata["clip"]["size_bytes"] = 999
    assert (await client.get(CLIP)).status_code == 502
    count = len(metadata["requests"])
    assert (await client.get("/api/v1/media/clips/not-a-uuid/media-info")).status_code == 422
    assert len(metadata["requests"]) == count
