"""Zero-duration annotations cannot block valid range clips or their lifecycle."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_daily_recordings import login
from test_event_export_reuse import ready
from test_event_exports import API, extractor, start
from test_event_thumbnails import existing, shared_thumbnails

from src.app.models.media_job import EventExportAlias, MediaClipJob
from src.app.schemas.media_service import ClipDetail, ClipGap

__all__ = ["extractor", "shared_thumbnails"]


def with_gaps(clip, *, positive=True):
    start = datetime.fromisoformat(clip["requested_start_at"].replace("Z", "+00:00"))
    point = start + timedelta(seconds=1)
    zero = {
        "start_at": point.isoformat(),
        "end_at": point.isoformat(),
        "reason_code": "UNKNOWN_CAPTURE_LOSS",
    }
    gap = {
        "start_at": (point + timedelta(seconds=1)).isoformat(),
        "end_at": (point + timedelta(seconds=2)).isoformat(),
        "reason_code": "CAMERA_DISCONNECTED",
    }
    clip["gaps"] = [copy.deepcopy(zero) for _ in range(9)] + ([gap] if positive else [])
    return ClipGap.model_validate(gap).model_dump(mode="json")


@pytest.mark.parametrize("positive", [True, False])
async def test_ready_range_with_zero_gaps_recovers_without_hiding_real_loss(
    client, test_db_session, extractor, positive
):
    headers = await login(client)
    event, _, created = await start(client, test_db_session, headers)
    assert created.status_code == 202
    clip = extractor["clips"][created.json()["clipId"]]
    ready(clip)
    gap = with_gaps(clip, positive=positive)
    original = copy.deepcopy(clip)
    response = await client.get(created.json()["statusPath"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    assert response.json()["coverage"] == "partial"
    assert response.json()["gaps"] == ([gap] if positive else [])
    assert response.json()["downloadPath"]
    detail = await client.get(f"/api/v1/media/clips/{clip['clip_id']}")
    assert detail.status_code == 200 and detail.json()["gaps"] == ([gap] if positive else [])
    found = await client.get(f"{API}/{event}/exports")
    assert found.status_code == 200 and found.json()["status"] == "ready"
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.snapshot["gaps"] == ([gap] if positive else [])
    assert "_rangeExport" in job.snapshot
    assert extractor["clips"][clip["clip_id"]] == original
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 1


@pytest.mark.parametrize("state", ["deleted", "expired"])
async def test_terminal_metadata_and_image_denial_survive_zero_gaps(
    client, test_db_session, extractor, state
):
    headers = await login(client)
    _, _, created = await start(client, test_db_session, headers)
    clip = extractor["clips"][created.json()["clipId"]]
    ready(clip)
    with_gaps(clip)
    # Go straight from the stored processing receipt to a terminal live state.
    clip.update(
        status=state,
        content_path=None,
        deleted_at=datetime.now(UTC).isoformat() if state == "deleted" else None,
    )
    response = await client.get(created.json()["statusPath"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == state and response.json()["downloadPath"] is None
    detail = await client.get(f"/api/v1/media/clips/{clip['clip_id']}")
    assert detail.status_code == 200 and detail.json()["status"] == state
    info = await client.get(f"/api/v1/media/clips/{clip['clip_id']}/media-info")
    assert info.status_code == 410 and info.json()["error"]["code"] == "CLIP_" + state.upper()
    again = await client.get(created.json()["statusPath"])
    assert again.status_code == 200 and again.json()["status"] == state


async def test_shared_thumbnail_accepts_zero_gaps_without_updating_job(
    client, test_db_session, shared_thumbnails
):
    data, changes = shared_thumbnails
    event, _, identifier = await existing(client, test_db_session, data)
    with_gaps(data["clips"][identifier])
    changes.update(
        status="unavailable",
        reason_code="NOT_PREPARED",
        **dict.fromkeys(("content_path", "width", "height", "size_bytes", "generated_at")),
    )
    job = await test_db_session.scalar(select(MediaClipJob))
    saved = copy.deepcopy(job.snapshot)
    await login(client, "operator")
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unavailable"
    assert response.json()["reason_code"] == "NOT_PREPARED"
    assert response.json()["clip_id"] == identifier
    assert all(r.method == "GET" for r in data["requests"])
    await test_db_session.refresh(job)
    assert job.snapshot == saved


@pytest.mark.parametrize(
    "mutation",
    [
        "reverse-gap",
        "zero-available",
        "reverse-available",
        "identity",
        "range",
        "attempt",
        "content",
    ],
)
async def test_invalid_range_guards_still_reject(client, test_db_session, extractor, mutation):
    headers = await login(client)
    _, _, created = await start(client, test_db_session, headers)
    clip = extractor["clips"][created.json()["clipId"]]
    ready(clip)
    with_gaps(clip)
    if mutation == "reverse-gap":
        gap = clip["gaps"][-1]
        gap["start_at"], gap["end_at"] = gap["end_at"], gap["start_at"]
    elif mutation in ("zero-available", "reverse-available"):
        interval = clip["available_intervals"][0]
        if mutation == "zero-available":
            interval["end_at"] = interval["start_at"]
        else:
            interval["start_at"], interval["end_at"] = interval["end_at"], interval["start_at"]
    elif mutation == "identity":
        clip["event_id"] = "unrelated-event"
    elif mutation == "range":
        clip["requested_start_at"] = "2000-01-01T00:00:00Z"
    elif mutation == "attempt":
        clip["attempt_count"] = 0
    else:
        clip["content_path"] = "https://untrusted.test/content"
    response = await client.get(created.json()["statusPath"])
    assert response.status_code == 502, response.text
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.status == "processing"


async def test_non_range_clip_and_short_positive_gap_are_preserved(
    client, test_db_session, extractor
):
    headers = await login(client)
    _, _, created = await start(client, test_db_session, headers)
    clip = extractor["clips"][created.json()["clipId"]]
    ready(clip)
    with_gaps(clip, positive=False)
    legacy = {**clip, "request_mode": "accepted_at"}
    assert len(ClipDetail.model_validate(legacy).gaps) == 9
    tiny = copy.deepcopy(clip["gaps"][0])
    point = datetime.fromisoformat(tiny["start_at"])
    tiny["end_at"] = (point + timedelta(microseconds=1)).isoformat()
    clip["gaps"].append(tiny)
    response = await client.get(created.json()["statusPath"])
    assert response.status_code == 200 and len(response.json()["gaps"]) == 1
    assert response.json()["coverage"] == "partial"
