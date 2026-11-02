"""Summary lists reduce wire size without bypassing recording evidence or permissions."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_daily_recordings import BASE, RID, START, login
from test_daily_recordings import camera as camera

from src.app.services.media_client import media_client


@pytest.mark.parametrize("username", ["operator", "admin"])
async def test_summary_preserves_metadata_cursor_and_public_paths(client, camera, username):
    assert (await client.get(BASE, params={"view": "summary"})).status_code == 401
    assert not camera.requests
    await login(client, username)
    params = {
        "from_date": "2026-10-01",
        "to_date": "2026-10-15",
        "limit": 7,
        "cursor": "opaque+/=",
        "status": "recording",
    }
    original = (await client.get(BASE, params=params)).json()
    full = await client.get(BASE, params={**params, "view": "full"})
    assert full.status_code == 200 and full.json() == original
    response = await client.get(BASE, params={**params, "view": "summary"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    page = response.json()
    assert page["view"] == "summary" and page["next_cursor"] == original["next_cursor"]
    item = page["items"][0]
    assert "gaps" not in item and "available_intervals" not in item
    assert item["gap_count"] == 1 and item["available_interval_count"] == 2
    metadata = {
        k: v for k, v in original["items"][0].items() if k not in ("gaps", "available_intervals")
    }
    assert {
        k: v for k, v in item.items() if k not in ("gap_count", "available_interval_count")
    } == metadata
    assert item["playback_path"] == f"/media/daily-recordings/{RID}/hls/index.m3u8"
    assert dict(camera.requests[-1].url.params) == {k: str(v) for k, v in params.items()}
    assert all(request.method == "GET" for request in camera.requests)


async def test_large_summary_is_small_and_detail_retains_every_interval(client, camera):
    count = 6000
    start = datetime.fromisoformat(START.replace("Z", "+00:00"))

    def stamp(seconds):
        return (start + timedelta(seconds=seconds)).astimezone(UTC).isoformat()

    camera.item.update(
        coverage="partial",
        coverage_as_of_at=stamp(count * 4),
        playable_until_at=stamp(count * 4),
        recorded_seconds=count,
        gaps=[
            {
                "start_at": stamp(i * 4 + 1),
                "end_at": stamp(i * 4 + 2),
                "reason_code": "CAPTURE_LOSS",
            }
            for i in range(count)
        ],
        available_intervals=[
            {
                "start_at": stamp(i * 4),
                "end_at": stamp(i * 4 + 1),
                "playback_start_seconds": i,
                "playback_end_seconds": i + 1,
            }
            for i in range(count)
        ],
    )
    before = deepcopy(camera.item)
    await login(client)
    full = await client.get(BASE)
    summary = await client.get(BASE, params={"view": "summary"})
    detail = await client.get(BASE + "/" + RID)
    assert full.status_code == summary.status_code == detail.status_code == 200
    item = summary.json()["items"][0]
    assert item["gap_count"] == item["available_interval_count"] == count
    assert item["recorded_seconds"] == count and item["coverage"] == "partial"
    assert len(summary.content) < len(full.content) / 100
    assert detail.json()["gaps"] == full.json()["items"][0]["gaps"]
    assert len(detail.json()["available_intervals"]) == count
    assert camera.item == before


@pytest.mark.parametrize("field", ["gaps", "available_intervals"])
@pytest.mark.parametrize("bad", ["missing", None])
async def test_summary_rejects_missing_or_null_evidence(client, camera, field, bad):
    if bad == "missing":
        del camera.item[field]
    else:
        camera.item[field] = None
    await login(client)
    response = await client.get(BASE, params={"view": "summary"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "MEDIA_BAD_RESPONSE"


@pytest.mark.parametrize("bad", ["camera", "path", "interval"])
async def test_summary_keeps_identity_path_and_interval_validation(client, camera, bad):
    if bad == "camera":
        camera.item["camera_id"] = "another-camera"
    elif bad == "path":
        camera.item["playback_path"] = "https://private.invalid/secret"
    else:
        camera.item["gaps"][0]["end_at"] = START
    await login(client)
    response = await client.get(BASE, params={"view": "summary"})
    assert response.status_code == 502
    assert "private.invalid" not in response.text and "secret" not in response.text


async def test_empty_arrays_are_known_zero_counts_without_changing_coverage(client, camera):
    camera.item.update(gaps=[], available_intervals=[], has_gaps=False)
    await login(client)
    response = await client.get(BASE, params={"view": "summary"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["gap_count"] == item["available_interval_count"] == 0
    assert item["recorded_seconds"] == 50 and item["coverage"] is None


async def test_zero_length_gap_is_still_counted_and_preserved_in_detail(client, camera):
    camera.item["gaps"][0]["end_at"] = camera.item["gaps"][0]["start_at"]
    await login(client)
    summary = await client.get(BASE, params={"view": "summary"})
    detail = await client.get(BASE + "/" + RID)
    assert summary.status_code == detail.status_code == 200
    assert summary.json()["items"][0]["gap_count"] == 1
    gap = detail.json()["gaps"][0]
    assert gap["start_at"] == gap["end_at"]


async def test_summary_retains_deleted_cleanup_and_safe_error_metadata(client, camera):
    camera.item.update(
        status="deleted",
        playback_state="unavailable",
        playback_path=None,
        export_state="failed",
        download_path=None,
        cleanup_state="failed",
        error={"code": "INTERNAL_ERROR", "message": "private-key"},
        cleanup_error={"code": "INTERNAL_ERROR", "message": "private-key"},
    )
    await login(client)
    response = await client.get(BASE, params={"view": "summary"})
    assert response.status_code == 200 and "private-key" not in response.text
    item = response.json()["items"][0]
    assert item["status"] == "deleted" and item["cleanup_state"] == "failed"
    assert item["playback_path"] is None and item["download_path"] is None


async def test_empty_summary_page_preserves_cursor(client, camera, monkeypatch):
    async def handle(request):
        camera.requests.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    await login(client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as remote:
        monkeypatch.setattr(media_client, "_client", remote)
        response = await client.get(BASE, params={"view": "summary"})
    assert response.status_code == 200
    assert response.json() == {"view": "summary", "items": [], "next_cursor": None}


async def test_summary_upstream_errors_keep_safe_contract_codes(client, camera):
    camera.error = (503, "INSUFFICIENT_STORAGE")
    await login(client)
    response = await client.get(BASE, params={"view": "summary"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INSUFFICIENT_STORAGE"
    assert "private-key" not in response.text


async def test_bad_view_does_not_call_media_and_logout_revokes_summary(client, camera):
    csrf = await login(client)
    assert (await client.get(BASE, params={"view": "bad"})).status_code == 422
    assert not camera.requests
    assert (await client.delete("/api/v1/session", headers=csrf)).status_code == 204
    assert (await client.get(BASE, params={"view": "summary"})).status_code == 401
    assert not camera.requests


async def test_summary_openapi_excludes_arrays_and_keeps_full_as_default(client):
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"][BASE]["get"]
    view = next(p for p in operation["parameters"] if p["name"] == "view")
    assert view["schema"]["default"] == "full"
    assert view["schema"]["enum"] == ["full", "summary"]
    summary = schema["components"]["schemas"]["DailyRecordingSummary"]
    assert "gaps" not in summary["properties"]
    assert "available_intervals" not in summary["properties"]
    assert {"gap_count", "available_interval_count"} <= set(summary["required"])
    assert "gaps" in schema["components"]["schemas"]["DailyRecording"]["properties"]
