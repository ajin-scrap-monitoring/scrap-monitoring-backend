"""Large daily metadata stays bounded and preserves cursor/snapshot evidence."""

from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from test_daily_recordings import BASE, RID, START, login, recording
from test_daily_recordings import camera as camera

from src.app.services.media_client import media_client


@pytest.fixture
async def paged_camera(camera, monkeypatch):
    full = recording()
    metadata = {k: v for k, v in full.items() if k not in ("gaps", "available_intervals")}
    remote = SimpleNamespace(
        metadata=metadata,
        pages={
            kind: {
                "recording_id": RID,
                "revision": 10,
                "snapshot": "opaque-snapshot",
                "items": deepcopy(full[field]),
                "total": 1_000_000,
                "next_cursor": "next+/=",
            }
            for kind, field in (("gaps", "gaps"), ("available-intervals", "available_intervals"))
        },
        requests=[],
        error=None,
    )

    async def handle(request):
        remote.requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "daily.test"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        path = request.url.path
        if remote.error and path.endswith(remote.error[0]):
            return httpx.Response(
                remote.error[1],
                json={"error": {"code": remote.error[2], "message": "private-secret"}},
            )
        if path.endswith("/cameras/camera-main/daily-recordings/summary"):
            return httpx.Response(200, json={"items": [remote.metadata], "next_cursor": "list+/="})
        if path.endswith(f"/{RID}/summary"):
            return httpx.Response(200, json=remote.metadata)
        for kind in remote.pages:
            if path.endswith(f"/{RID}/{kind}"):
                return httpx.Response(200, json=remote.pages[kind])
        # A legacy full read must not be part of any new summary/page request.
        return httpx.Response(503, json={"error": {"code": "DAILY_RESPONSE_TOO_LARGE"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        yield remote


PATHS = ["/summary", f"/{RID}/summary", f"/{RID}/gaps", f"/{RID}/available-intervals"]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("username", ["operator", "admin"])
async def test_new_reads_require_session_and_preserve_evidence(
    client, paged_camera, path, username
):
    assert (await client.get(BASE + path)).status_code == 401
    assert not paged_camera.requests
    csrf = await login(client, username)
    response = await client.get(BASE + path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    if path.endswith("summary"):
        item = response.json()["items"][0] if path == "/summary" else response.json()
        assert item["revision"] == 10 and item["has_gaps"] is True
        assert item["coverage"] is None and item["recorded_seconds"] == 50
        assert (
            not {"gaps", "available_intervals", "gap_count", "available_interval_count"}
            & item.keys()
        )
        assert item["playback_path"] == f"/media/daily-recordings/{RID}/hls/index.m3u8"
    else:
        page = response.json()
        assert page["total"] == 1_000_000 and page["next_cursor"] == "next+/="
        assert page["snapshot"] == "opaque-snapshot" and page["revision"] == 10
        assert len(paged_camera.requests) == 2  # One ownership check and exactly one page.
    await client.delete("/api/v1/session", headers=csrf)
    before = len(paged_camera.requests)
    assert (await client.get(BASE + path)).status_code == 401
    assert len(paged_camera.requests) == before


async def test_summary_list_forwards_filters_without_reusing_full_response(client, paged_camera):
    await login(client)
    params = {
        "from_date": "2026-10-01",
        "to_date": "2026-10-15",
        "status": "recording",
        "limit": 7,
        "cursor": "opaque+/=",
    }
    response = await client.get(BASE + "/summary", params=params)
    assert response.status_code == 200 and response.json()["next_cursor"] == "list+/="
    assert len(paged_camera.requests) == 1
    assert dict(paged_camera.requests[0].url.params) == {k: str(v) for k, v in params.items()}


@pytest.mark.parametrize("kind", ["gaps", "available-intervals"])
async def test_page_preserves_cursor_total_and_original_values(client, paged_camera, kind):
    await login(client)
    before = deepcopy(paged_camera.pages[kind])
    response = await client.get(BASE + f"/{RID}/{kind}", params={"limit": 7, "cursor": "signed+/="})
    assert response.status_code == 200
    assert response.json()["items"] == [
        {k: v.replace(".000Z", "Z") if isinstance(v, str) else v for k, v in item.items()}
        for item in before["items"]
    ]
    assert dict(paged_camera.requests[-1].url.params) == {"limit": "7", "cursor": "signed+/="}
    assert paged_camera.pages[kind] == before
    # A short page with a next cursor is not silently treated as the end.
    assert response.json()["next_cursor"] == before["next_cursor"]
    paged_camera.pages[kind].update(next_cursor=None)
    final = await client.get(BASE + f"/{RID}/{kind}", params={"cursor": "last+/="})
    assert final.status_code == 200 and final.json()["next_cursor"] is None


@pytest.mark.parametrize(
    "path,params",
    [
        ("/summary", {"limit": 0}),
        ("/summary", {"limit": 101}),
        ("/summary", {"from_date": "2026-10-15", "to_date": "2026-10-01"}),
        ("/summary", {"from_date": "2026-1-1"}),
        ("/summary", {"cursor": ""}),
        (f"/{RID}/gaps", {"limit": 501}),
        (f"/{RID}/gaps", {"limit": 0}),
        (f"/{RID}/available-intervals", {"cursor": "x" * 4097}),
    ],
)
async def test_invalid_params_do_not_call_media(client, paged_camera, path, params):
    await login(client)
    assert (await client.get(BASE + path, params=params)).status_code == 422
    assert not paged_camera.requests


@pytest.mark.parametrize("path", ["/summary", f"/{RID}/summary", f"/{RID}/gaps"])
@pytest.mark.parametrize("bad", ["camera", "path", "window"])
async def test_metadata_identity_paths_and_window_remain_validated(client, paged_camera, path, bad):
    if bad == "camera":
        paged_camera.metadata["camera_id"] = "another-camera"
    elif bad == "path":
        paged_camera.metadata["playback_path"] = "https://private.invalid/private-secret"
    else:
        paged_camera.metadata["coverage_as_of_at"] = "2027-01-01T00:00:00Z"
    await login(client)
    response = await client.get(BASE + path)
    assert response.status_code == 502 and "private-secret" not in response.text
    assert len(paged_camera.requests) == 1


@pytest.mark.parametrize("stage", ["metadata", "page"])
async def test_detail_and_pages_reject_wrong_recording(client, paged_camera, stage):
    target = paged_camera.metadata if stage == "metadata" else paged_camera.pages["gaps"]
    target["recording_id"] = "550e8400-e29b-41d4-a716-446655440001"
    await login(client)
    response = await client.get(BASE + f"/{RID}/gaps")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "MEDIA_IDENTITY_MISMATCH"


async def test_page_revision_race_returns_restart_error(client, paged_camera):
    paged_camera.pages["gaps"]["revision"] = 11
    await login(client)
    response = await client.get(BASE + f"/{RID}/gaps")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DAILY_SNAPSHOT_CHANGED"


@pytest.mark.parametrize(
    "bad", ["reverse", "outside", "order", "total", "empty", "limit", "offset"]
)
async def test_malformed_page_is_not_forwarded_as_success(client, paged_camera, bad):
    page = paged_camera.pages["available-intervals"]
    if bad == "reverse":
        page["items"][0]["end_at"] = "2026-10-13T14:00:00Z"
    elif bad == "outside":
        page["items"][0]["start_at"] = "2026-10-13T14:59:59Z"
    elif bad == "order":
        page["items"].reverse()
    elif bad == "total":
        page["total"] = 1
    elif bad == "empty":
        page["items"] = []
    elif bad == "offset":
        page["items"][0]["playback_end_seconds"] = 0
    await login(client)
    response = await client.get(
        BASE + f"/{RID}/available-intervals", params={"limit": 1 if bad == "limit" else 100}
    )
    assert response.status_code == 502 and response.json()["error"]["code"] == "MEDIA_BAD_RESPONSE"


async def test_empty_and_zero_wall_clock_pages_preserve_meaning(client, paged_camera):
    await login(client)
    gaps = paged_camera.pages["gaps"]
    gaps.update(items=[], total=0, next_cursor=None)
    assert (await client.get(BASE + f"/{RID}/gaps")).json()["total"] == 0
    interval = paged_camera.pages["available-intervals"]["items"][0]
    interval.update(start_at=START, end_at=START)
    response = await client.get(BASE + f"/{RID}/available-intervals")
    assert response.status_code == 200
    assert response.json()["items"][0]["playback_end_seconds"] == 20


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "INVALID_CURSOR"),
        (409, "DAILY_SNAPSHOT_CHANGED"),
        (503, "SERVICE_BUSY"),
        (503, "DAILY_RESPONSE_TOO_LARGE"),
        (503, "CATALOG_METADATA_ERROR"),
        (404, "DAILY_RECORDING_NOT_FOUND"),
    ],
)
async def test_page_errors_keep_status_safe_code_and_no_store(client, paged_camera, status, code):
    paged_camera.error = ("/gaps", status, code)
    await login(client)
    response = await client.get(BASE + f"/{RID}/gaps")
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store" and "private-secret" not in response.text
    assert len(paged_camera.requests) == 2  # No retries or mutation on failure.


async def test_deleted_summary_keeps_cleanup_and_sanitizes_error(client, paged_camera):
    paged_camera.metadata.update(
        status="deleted",
        playback_path=None,
        playback_state="unavailable",
        cleanup_state="failed",
        cleanup_error={"code": "INTERNAL_ERROR", "message": "private-secret"},
    )
    await login(client)
    response = await client.get(BASE + f"/{RID}/summary")
    assert response.status_code == 200 and "private-secret" not in response.text
    assert response.json()["status"] == "deleted" and response.json()["space_reclaimed"] is None


async def test_new_openapi_has_separate_evidence_types_and_bounded_pages(client):
    schema = (await client.get("/openapi.json")).json()
    metadata = schema["components"]["schemas"]["DailyRecordingMetadata"]
    assert "revision" in metadata["required"]
    assert (
        not {"gaps", "available_intervals", "gap_count", "available_interval_count"}
        & metadata["properties"].keys()
    )
    for path in PATHS:
        path = path.replace(RID, "{recording_id}")
        operation = schema["paths"][BASE + path]["get"]
        assert {"400", "409", "503"} <= operation["responses"].keys()
    legacy = schema["components"]["schemas"]["DailyRecordingSummary"]
    assert {"gap_count", "available_interval_count"} <= set(legacy["required"])
