"""Shared event thumbnails preserve private exports and never create media work."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_daily_recordings import login
from test_event_export_reuse import ready
from test_event_exports import API, extractor, start
from test_event_recordings import BASE, alert

from src.app.core.config import get_settings
from src.app.models.media_job import EventExportAlias, MediaClipJob
from src.app.services.event_export_service import CONTEXT_KEY, export_identifier
from src.app.services.media_client import media_client

__all__ = ["extractor"]


@pytest.fixture
async def shared_thumbnails(extractor, monkeypatch):
    original = media_client._client
    changes = {}

    async def handle(request):
        if request.url.path.endswith("/thumbnail-info"):
            assert "authorization" not in request.headers and "cookie" not in request.headers
            extractor["requests"].append(request)
            identifier = request.url.path.split("/")[-2]
            info = {
                "clip_id": identifier,
                "camera_id": "camera-main",
                "status": "ready",
                "representative_at": None,
                "time_basis": None,
                "content_path": f"/api/v1/clips/{identifier}/thumbnail",
                "width": 320,
                "height": 240,
                "size_bytes": 1024,
                "generated_at": datetime.now(UTC).isoformat(),
                "reason_code": "TIMELINE_UNAVAILABLE",
            }
            info.update(changes)
            return httpx.Response(200, json=info)
        if request.method == "GET" and "authorization" not in request.headers:
            # Thumbnail parents use the tokenless media contract; export fixtures
            # retain their separate authenticated request assertions unchanged.
            assert "cookie" not in request.headers
            extractor["requests"].append(request)
            if extractor["error"]:
                status, code = extractor["error"]
                return httpx.Response(
                    status, json={"error": {"code": code, "message": "private-secret"}}
                )
            return httpx.Response(200, json=extractor["clips"][request.url.path.rsplit("/", 1)[1]])
        return await original._transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(media_client, "_client", client)
        yield extractor, changes


async def existing(client, db, data):
    headers = await login(client, "admin")
    event, body, response = await start(client, db, headers)
    assert response.status_code == 202, response.text
    clip_id = response.json()["clipId"]
    ready(data["clips"][clip_id])
    assert (await client.get(f"{API}/{event}/exports")).json()["status"] == "ready"
    data["requests"].clear()
    return event, body, clip_id


async def test_submillisecond_event_uses_same_normalization_as_export(
    client, test_db_session, shared_thumbnails, monkeypatch
):
    from test_event_exports import payload

    data, _ = shared_thumbnails
    headers = await login(client, "admin")
    event = await alert(test_db_session, BASE.replace(microsecond=123456))
    created = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert created.status_code == 202, created.text
    clip = created.json()["clipId"]
    ready(data["clips"][clip])
    assert (await client.get(f"{API}/{event}/exports")).json()["status"] == "ready"
    data["requests"].clear()
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200 and response.json()["clip_id"] == clip, response.text


async def test_shared_thumbnail_keeps_exports_private_and_db_unchanged(
    client, test_db_session, shared_thumbnails
):
    data, _ = shared_thumbnails
    event, body, clip = await existing(client, test_db_session, data)
    job = await test_db_session.scalar(select(MediaClipJob))
    snapshot = copy.deepcopy(job.snapshot)
    await login(client, "operator")
    assert (await client.get(f"{API}/{event}/exports")).json() is None
    assert (await client.get(f"{API}/{event}/exports/{body['requestId']}")).status_code == 404
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["clip_id"] == clip
    assert info["content_path"] == f"/media/clips/{clip}/thumbnail"
    assert info["representative_at"] is None and info["time_basis"] is None
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "requestId" not in info and "owner" not in info
    assert all(request.method == "GET" for request in data["requests"])
    assert len(data["requests"]) == 2
    await test_db_session.refresh(job)
    assert job.snapshot == snapshot and job.status == "ready" and job.attempts == 1
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 1


async def test_permissions_missing_and_context_validation(
    client, test_db_session, shared_thumbnails
):
    data, _ = shared_thumbnails
    event = await alert(test_db_session)
    path = f"{API}/{event}/thumbnail-info"
    assert (await client.get(path)).status_code == 401
    await login(client, "operator")
    response = await client.get(path)
    assert response.status_code == 200 and response.json() is None
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get(f"{API}/evt-alt-2147483648/thumbnail-info")).status_code == 404
    assert (await client.get(path, params={"pit_id": "other"})).status_code == 404
    for params in (
        {"before_seconds": 301},
        {"after_seconds": -1},
        {"before_seconds": 0, "after_seconds": 0},
    ):
        assert (await client.get(path, params=params)).status_code == 422
    assert data["requests"] == []


@pytest.mark.parametrize("stable", [False, True])
async def test_list_and_detail_provide_scoped_path_without_media(
    client, test_db_session, shared_thumbnails, stable
):
    data, _ = shared_thumbnails
    event = await alert(test_db_session)
    await login(client)
    response = await client.get(
        API,
        params={
            "pit_id": "pit-demo",
            "from": (BASE - timedelta(days=1)).isoformat(),
            "to": (BASE + timedelta(days=1)).isoformat(),
            "stable": stable,
            "before_seconds": 10,
            "after_seconds": 20,
        },
    )
    assert response.status_code == 200, response.text
    item = next(item for item in response.json()["items"] if item["eventId"] == event)
    path = item["thumbnailInfoPath"]
    assert (
        path == f"{API}/{event}/thumbnail-info?pit_id=pit-demo&before_seconds=10&after_seconds=20"
    )
    assert (await client.get(path)).json() is None
    assert data["requests"] == []


@pytest.mark.parametrize(
    "mutation", ["camera", "pit", "source", "event", "range", "owner", "category", "status", "clip"]
)
async def test_unrelated_or_invalid_saved_candidates_not_queried(
    client, test_db_session, shared_thumbnails, mutation
):
    data, _ = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    job = await test_db_session.scalar(select(MediaClipJob))
    snapshot = copy.deepcopy(job.snapshot)
    if mutation == "camera":
        job.camera_id = "other-camera"
    elif mutation == "pit":
        job.pit_id = "other-pit"
    elif mutation == "category":
        job.category = "other"
    elif mutation == "status":
        job.status = "processing"
    elif mutation == "clip":
        job.clip_id = "invalid-uuid"
    else:
        key = {"source": "source", "event": "event_id", "range": "end", "owner": "owner"}[mutation]
        snapshot[CONTEXT_KEY][key] = "other"
        job.snapshot = snapshot
    await test_db_session.commit()
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200 and response.json() is None, response.text
    assert data["requests"] == []


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"event_id": "other-event"}, 502),
        ({"camera_id": "other-camera"}, 200),
        ({"clip_id": str(uuid4())}, 502),
        ({"requested_start_at": "2000-01-01T00:00:00Z"}, 502),
        ({"status": "processing", "content_path": None}, 409),
        ({"status": "deleted", "content_path": None}, 200),
        ({"expires_at": "2000-01-01T00:00:00Z"}, 200),
    ],
)
async def test_live_parent_is_verified_before_thumbnail(
    client, test_db_session, shared_thumbnails, mutation, expected
):
    data, _ = shared_thumbnails
    event, _, clip = await existing(client, test_db_session, data)
    data["clips"][clip].update(mutation)
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json() is None
    assert len(data["requests"]) == 1


async def test_transient_media_failure_is_not_empty_result(
    client, test_db_session, shared_thumbnails
):
    data, _ = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    data["error"] = (503, "RECORDING_STORAGE_ERROR")
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 503
    assert "private-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"
    data["error"] = None
    assert (await client.get(f"{API}/{event}/thumbnail-info")).status_code == 200


async def test_thumbnail_path_validation_preserved(client, test_db_session, shared_thumbnails):
    data, changes = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    changes["content_path"] = "https://outside.test/private.jpg"
    assert (await client.get(f"{API}/{event}/thumbnail-info")).status_code == 502


async def duplicate_job(db, data, status):
    original = await db.scalar(select(MediaClipJob).order_by(MediaClipJob.id))
    snapshot = copy.deepcopy(original.snapshot)
    identifier, request_id = str(uuid4()), uuid4()
    event_id = export_identifier(snapshot[CONTEXT_KEY]["owner"], request_id)
    snapshot[CONTEXT_KEY]["request_id"] = str(request_id)
    snapshot.update(clip_id=identifier, event_id=event_id)
    db.add(
        MediaClipJob(
            event_id=event_id,
            camera_id=original.camera_id,
            pit_id=original.pit_id,
            category=original.category,
            occurred_at=original.occurred_at,
            clip_id=identifier,
            status="ready",
            attempts=1,
            snapshot=snapshot,
        )
    )
    await db.commit()
    data["clips"][identifier] = {
        **data["clips"][original.clip_id],
        "clip_id": identifier,
        "event_id": event_id,
        "status": status,
        "content_path": f"/api/v1/clips/{identifier}/content" if status == "ready" else None,
    }
    return identifier


async def test_newest_deleted_clip_falls_back_to_older_ready(
    client, test_db_session, shared_thumbnails
):
    data, _ = shared_thumbnails
    event, _, older = await existing(client, test_db_session, data)
    deleted = await duplicate_job(test_db_session, data, "deleted")
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200 and response.json()["clip_id"] == older
    assert str(data["requests"][0].url).endswith(deleted)
    assert len(data["requests"]) == 3 and all(r.method == "GET" for r in data["requests"])


async def test_fallback_attempts_are_bounded(client, test_db_session, shared_thumbnails):
    data, _ = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    for _ in range(4):
        await duplicate_job(test_db_session, data, "deleted")
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200 and response.json() is None
    assert len(data["requests"]) == 3


async def test_transient_failure_does_not_fall_back_to_stale_result(
    client, test_db_session, shared_thumbnails
):
    data, _ = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    await duplicate_job(test_db_session, data, "ready")
    data["error"] = (503, "RECORDING_STORAGE_ERROR")
    assert (await client.get(f"{API}/{event}/thumbnail-info")).status_code == 503
    assert len(data["requests"]) == 1


async def test_parent_and_info_share_timeout_budget(
    client, test_db_session, shared_thumbnails, monkeypatch
):
    data, _ = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    original = media_client._request
    monkeypatch.setattr(get_settings(), "MEDIA_REQUEST_TIMEOUT_SECONDS", 0.05)

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0.03)
        return await original(*args, **kwargs)

    monkeypatch.setattr(media_client, "_request", delayed)
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 504 and response.json()["error"]["code"] == "MEDIA_TIMEOUT"
    assert len(data["requests"]) == 1


async def test_different_event_window_cannot_borrow_thumbnail(
    client, test_db_session, shared_thumbnails
):
    data, _ = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    response = await client.get(f"{API}/{event}/thumbnail-info", params={"before_seconds": 10})
    assert response.status_code == 200 and response.json() is None
    assert data["requests"] == []


async def test_unprepared_thumbnail_never_triggers_post(client, test_db_session, shared_thumbnails):
    data, changes = shared_thumbnails
    event, _, _ = await existing(client, test_db_session, data)
    changes.update(
        status="unavailable",
        reason_code="NOT_PREPARED",
        **dict.fromkeys(("content_path", "width", "height", "size_bytes", "generated_at")),
    )
    response = await client.get(f"{API}/{event}/thumbnail-info")
    assert response.status_code == 200 and response.json()["reason_code"] == "NOT_PREPARED"
    assert all(r.method == "GET" for r in data["requests"])
