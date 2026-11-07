"""One occurrence-date image is shared across event categories without media jobs."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_daily_recordings import RID, login
from test_event_recordings import API, alert, archive, cycle
from test_thumbnails import thumbnails

from src.app.core.config import get_settings
from src.app.models.metric import ScrapMetric
from src.app.services.media_client import media_client

__all__ = ["archive", "thumbnails"]
PATH = "/api/v2/recordings/daily/by-date/2026-10-14/thumbnail-info"


@pytest.fixture
async def calendar_thumbnails(thumbnails, monkeypatch):
    data = thumbnails
    data["page"] = {"items": [copy.deepcopy(data["day"])], "next_cursor": None}
    data["page_error"] = None
    original = media_client._client

    async def handle(request):
        if request.url.path.endswith("/daily-recordings"):
            data["requests"].append(request)
            assert request.method == "GET"
            assert "cookie" not in request.headers and "authorization" not in request.headers
            assert dict(request.url.params) == {
                "from_date": "2026-10-14",
                "to_date": "2026-10-15",
                "limit": "2",
            }
            if data["page_error"]:
                status, code = data["page_error"]
                return httpx.Response(status, json={"error": {"code": code, "message": "secret"}})
            return httpx.Response(200, json=data["page"])
        return await original._transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr(media_client, "_client", transport)
        yield data


@pytest.mark.parametrize("stable", [False, True])
async def test_categories_share_occurrence_date_path_without_media_reads(
    client, test_db_session, archive, stable
):
    midnight = datetime(2026, 10, 13, 15, tzinfo=UTC)
    await alert(test_db_session, midnight)
    await alert(test_db_session, midnight - timedelta(microseconds=1))
    await cycle(test_db_session, midnight, midnight + timedelta(days=1, minutes=1))
    test_db_session.add(
        ScrapMetric(
            pit_id="pit-demo",
            measurement_id="synthetic-image-error",
            measured_at=midnight,
            lidar1_distance_cm=0,
            calculated_height_cm=0,
            fill_ratio_percent=0,
            is_valid=False,
        )
    )
    await test_db_session.commit()
    await login(client)
    r = await client.get(
        API,
        params={
            "from": (midnight - timedelta(days=1)).isoformat(),
            "to": (midnight + timedelta(days=2)).isoformat(),
            "stable": stable,
        },
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    same_day = [x for x in items if x["occurredAt"] == "2026-10-13T15:00:00Z"]
    assert {x["category"] for x in same_day} == {"collection", "alert", "error"}
    assert all(x["dailyThumbnailInfoPath"] == PATH for x in same_day)
    previous = next(x for x in items if x["occurredAt"] != "2026-10-13T15:00:00Z")
    assert previous["dailyThumbnailInfoPath"].endswith("2026-10-13/thumbnail-info")
    assert all("/events/" in x["thumbnailInfoPath"] for x in items)
    assert archive["requests"] == []


async def test_shared_image_uses_existing_daily_contract(client, calendar_thumbnails):
    data = calendar_thumbnails
    assert (await client.get(PATH)).status_code == 401
    assert data["requests"] == []
    await login(client)
    r = await client.get(PATH)
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["recording_id"] == RID and "clip_id" not in info
    assert info["content_path"] == f"/media/daily-recordings/{RID}/thumbnail"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert len(data["requests"]) == 3
    assert all(x.method == "GET" for x in data["requests"])


async def test_missing_then_available_date_is_not_cached_as_missing(client, calendar_thumbnails):
    data = calendar_thumbnails
    data["page"]["items"] = []
    await login(client)
    assert (await client.get(PATH)).json() is None
    assert len(data["requests"]) == 1
    data["page"]["items"] = [copy.deepcopy(data["day"])]
    assert (await client.get(PATH)).json()["status"] == "ready"


@pytest.mark.parametrize("mutation", ["date", "camera", "duplicate", "cursor"])
async def test_unrelated_ambiguous_or_paginated_dates_fail_closed(
    client, calendar_thumbnails, mutation
):
    data = calendar_thumbnails
    item = data["page"]["items"][0]
    if mutation == "date":
        item["recording_date"] = "2026-10-15"
        item["window_start_at"] = "2026-10-14T15:00:00Z"
        item["window_end_at"] = "2026-10-15T15:00:00Z"
    elif mutation == "camera":
        item["camera_id"] = "other-camera"
    elif mutation == "duplicate":
        data["page"]["items"].append(copy.deepcopy(item))
    else:
        data["page"]["next_cursor"] = "unexpected-page"
    await login(client)
    assert (await client.get(PATH)).status_code == 502
    assert len(data["requests"]) == 1


@pytest.mark.parametrize("state", ["deleted", "expired"])
async def test_deleted_or_expired_parent_invalidates_shared_image(
    client, calendar_thumbnails, state
):
    data = calendar_thumbnails
    await login(client)
    assert (await client.get(PATH)).json()["status"] == "ready"
    data["day"].update(status=state, playback_path=None, download_path=None)
    data["requests"].clear()
    r = await client.get(PATH)
    assert r.status_code == 410, r.text
    assert len(data["requests"]) == 2  # Fresh parent check precedes image info.


@pytest.mark.parametrize(
    "status,reason",
    [
        ("unavailable", "NOT_PREPARED"),
        ("unavailable", "DAILY_MP4_REQUIRED"),
        ("failed", "GENERATION_FAILED"),
        ("pending", None),
    ],
)
async def test_not_ready_states_are_returned_without_preparation(
    client, calendar_thumbnails, status, reason
):
    data = calendar_thumbnails
    data["changes"].update(
        status=status,
        reason_code=reason,
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
    await login(client)
    r = await client.get(PATH)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == status and r.json()["reason_code"] == reason
    assert all(x.method == "GET" for x in data["requests"])


async def test_errors_remain_errors_and_are_sanitized(client, calendar_thumbnails):
    data = calendar_thumbnails
    data["page_error"] = (503, "MEDIA_UNAVAILABLE")
    await login(client)
    r = await client.get(PATH)
    assert r.status_code == 503 and "secret" not in r.text
    assert r.json() is not None


@pytest.mark.parametrize("date", ["2026-02-30", "20261014", "9999-12-31"])
async def test_invalid_calendar_dates_do_not_contact_media(client, calendar_thumbnails, date):
    await login(client)
    assert (await client.get(PATH.replace("2026-10-14", date))).status_code == 422
    assert calendar_thumbnails["requests"] == []


async def test_whole_lookup_has_one_time_budget(client, calendar_thumbnails, monkeypatch):
    async def slow(_):
        await asyncio.sleep(1)

    from src.app.services.daily_recording_service import daily_recording_service

    monkeypatch.setattr(daily_recording_service, "list", slow)
    monkeypatch.setattr(get_settings(), "MEDIA_REQUEST_TIMEOUT_SECONDS", 0.01)
    await login(client)
    r = await client.get(PATH)
    assert r.status_code == 504 and r.json()["error"]["code"] == "MEDIA_TIMEOUT"


async def test_date_is_rechecked_on_fresh_parent_before_thumbnail_read(client, calendar_thumbnails):
    from src.app.schemas.daily_recording import DailyRecording

    data = calendar_thumbnails
    parent = data["day"]
    parent["recording_date"] = "2026-10-15"
    for key in ("window_start_at", "window_end_at", "coverage_as_of_at", "playable_until_at"):
        parent[key] = (datetime.fromisoformat(parent[key]) + timedelta(days=1)).isoformat()
    for interval in parent["gaps"] + parent["available_intervals"]:
        for key in ("start_at", "end_at"):
            interval[key] = (datetime.fromisoformat(interval[key]) + timedelta(days=1)).isoformat()
    # The fresh parent is internally valid; only its occurrence-date binding is wrong.
    DailyRecording.model_validate(parent)
    await login(client)
    r = await client.get(PATH)
    assert r.status_code == 502
    assert len(data["requests"]) == 2
