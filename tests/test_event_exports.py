"""On-demand extraction, durable identity, scope and partial coverage."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_daily_recordings import login
from test_event_recordings import BASE, alert, cycle

from src.app.core.config import get_settings
from src.app.models.media_job import MediaClipJob
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.event_export_service import CONTEXT_KEY
from src.app.services.media_client import media_client
from src.app.services.media_job_service import MediaJobWorker, media_job_worker

API = "/api/v2/recordings/events"


@pytest.fixture
async def extractor(monkeypatch):
    settings = get_settings()
    for key, value in {
        "MEDIA_SERVICE_URL": "http://media.test/api/v1",
        "MEDIA_API_TOKEN": SecretStr("synthetic-media-token"),
        "MEDIA_PIT_ID": "pit-demo",
        "MEDIA_CAMERA_ID": "camera-main",
        "MEDIA_V2_EVENT_CLIPS_ENABLED": False,
        "EDGE_V2_MONITORING_SOURCES": [
            MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")
        ],
    }.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(media_job_worker, "lock", asyncio.Lock())
    data = {"requests": [], "clips": {}, "lost": False, "mismatch": None, "error": None}

    async def handle(request):
        data["requests"].append(request)
        assert request.headers["authorization"] == "Bearer synthetic-media-token"
        assert "cookie" not in request.headers
        if data["error"]:
            status, code = data["error"]
            return httpx.Response(
                status, json={"error": {"code": code, "message": "private-secret"}}
            )
        if request.method == "DELETE":
            clip = data["clips"][request.url.path.rsplit("/", 1)[1]]
            clip.update(status="deleted", content_path=None)
            return httpx.Response(204)
        if request.method == "POST":
            body = json.loads(request.content)
            assert "event_at" not in body
            key = str(uuid5(NAMESPACE_URL, body["event_id"]))
            code = 200 if key in data["clips"] else 202
            if key not in data["clips"]:
                data["clips"][key] = {
                    "clip_id": key,
                    "event_id": body["event_id"],
                    "camera_id": body["camera_id"],
                    "request_mode": "time_range",
                    "attempt_count": 1,
                    "status": "processing",
                    "accepted_at": datetime.now(UTC).isoformat(),
                    "requested_start_at": body["start_at"],
                    "requested_end_at": body["end_at"],
                    "coverage": "partial",
                    "recording_policy": {
                        "pre_roll_seconds": 30,
                        "post_roll_seconds": 30,
                        "retention_days": 30,
                    },
                    "actual_start_at": body["start_at"],
                    "actual_end_at": body["end_at"],
                    "gaps": [],
                    "available_intervals": [],
                    "completed_at": None,
                    "expires_at": None,
                    "deleted_at": None,
                    "content_path": None,
                    "error": None,
                    "extraction_mode": "stream_copy",
                    "size_bytes": None,
                }
            clip = data["clips"][key]
            if body.get("retry_failed") and clip["status"] == "failed":
                clip.update(
                    status="processing", error=None, attempt_count=clip["attempt_count"] + 1
                )
                code = 202
            if data["lost"]:
                data["lost"] = False
                raise httpx.ReadTimeout("Response lost after acceptance", request=request)
        else:
            assert request.method == "GET"
            clip = data["clips"][request.url.path.rsplit("/", 1)[1]]
            code = 200
        return httpx.Response(code, json={**clip, **(data["mismatch"] or {})})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as mock:
        monkeypatch.setattr(media_client, "_client", mock)
        yield data


def payload(**kwargs):
    return {"requestId": str(uuid4()), **kwargs}


async def start(client, db, headers):
    event_id, body = await alert(db), payload()
    r = await client.post(f"{API}/{event_id}/exports", json=body, headers=headers)
    return event_id, body, r


async def test_permissions_validation_and_absent_event(client, test_db_session, extractor):
    url = f"{API}/evt-alt-1/exports"
    assert (await client.post(url, json=payload())).status_code == 401
    headers = await login(client, "operator")
    assert (await client.post(url, json=payload())).status_code == 403
    for body in [
        payload(retryFailed="true"),
        payload(beforeSeconds=True),
        payload(start_at=BASE.isoformat()),
        payload(beforeSeconds=301),
        {"requestId": "bad"},
        payload(beforeSeconds=0, afterSeconds=0),
    ]:
        assert (await client.post(url, json=body, headers=headers)).status_code == 422
    missing = await client.post(
        f"{API}/evt-alt-2147483648/exports", json=payload(), headers=headers
    )
    assert missing.status_code == 404
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0
    assert extractor["requests"] == []


async def test_viewer_duplicate_and_exact_context(client, test_db_session, extractor):
    headers = await login(client, "operator")
    event, body, first = await start(client, test_db_session, headers)
    assert first.status_code == 202, first.text
    result = first.json()
    assert first.headers["location"] == result["statusPath"]
    assert first.headers["cache-control"] == "no-store" and result["downloadPath"] is None
    again = await client.post(f"{API}/{event}/exports", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["clipId"] == result["clipId"]
    assert len(extractor["clips"]) == 1
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1
    sent = json.loads(extractor["requests"][0].content)
    assert sent["start_at"] == "2026-10-14T02:59:30.000Z"
    assert sent["end_at"] == "2026-10-14T03:00:30.000Z"
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 1


async def test_exact_collection_window(client, test_db_session, extractor):
    headers = await login(client)
    event = await cycle(test_db_session, BASE, BASE + timedelta(seconds=120))
    r = await client.post(
        f"{API}/{event}/exports", json=payload(beforeSeconds=100, afterSeconds=50), headers=headers
    )
    assert r.status_code == 202, r.text
    sent = json.loads(extractor["requests"][0].content)
    assert sent["start_at"] == "2026-10-14T03:00:00.000Z"
    assert sent["end_at"] == "2026-10-14T03:02:00.000Z"


@pytest.mark.parametrize("seconds,code", [(None, 409), (3601, 422), (0, 422)])
async def test_unconfirmed_or_oversized_window(client, test_db_session, extractor, seconds, code):
    headers = await login(client)
    end = BASE + timedelta(seconds=seconds) if seconds is not None else None
    event = await cycle(test_db_session, BASE, end)
    r = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert r.status_code == code, r.text
    assert extractor["requests"] == []


async def test_partial_metadata_and_download_expiry(client, test_db_session, extractor):
    headers = await login(client)
    _, _, r = await start(client, test_db_session, headers)
    item = r.json()
    clip = extractor["clips"][item["clipId"]]
    clip.update(
        status="ready",
        content_path=f"/api/v1/clips/{item['clipId']}/content",
        completed_at=datetime.now(UTC).isoformat(),
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        size_bytes=4096,
        actual_start_at="2026-10-14T02:59:40.000Z",
        gaps=[
            {
                "start_at": "2026-10-14T02:59:30.000Z",
                "end_at": "2026-10-14T02:59:40.000Z",
                "reason_code": "CAMERA_DISCONNECTED",
            }
        ],
        available_intervals=[
            {"start_at": "2026-10-14T02:59:40.000Z", "end_at": "2026-10-14T03:00:30.000Z"}
        ],
    )
    detail = await client.get(item["statusPath"])
    assert detail.status_code == 200, detail.text
    out = detail.json()
    assert out["downloadPath"] == f"/media/clips/{item['clipId']}/content?download=true"
    assert out["coverage"] == "partial" and out["sizeBytes"] == 4096
    assert out["actualFrom"] == "2026-10-14T02:59:40Z"
    assert out["gaps"][0]["reason_code"] == "CAMERA_DISCONNECTED"
    assert len(out["availableIntervals"]) == 1
    clip["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert (await client.get(item["statusPath"])).json()["downloadPath"] is None
    for state in ["deleted", "expired"]:
        clip.update(status=state, content_path=None)
        assert (await client.get(item["statusPath"])).json()["downloadPath"] is None
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1


async def test_lost_response_worker_recovery_get_never_posts(client, test_db_session, extractor):
    headers = await login(client)
    extractor["lost"] = True
    event, body, r = await start(client, test_db_session, headers)
    assert r.status_code == 504
    path = f"{API}/{event}/exports/{body['requestId']}"
    assert (await client.get(path)).json()["status"] == "pending"
    assert len(extractor["requests"]) == 1
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    assert job.clip_id is None
    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await test_db_session.commit()
    await MediaJobWorker().run_once(test_db_session)
    assert len(extractor["clips"]) == 1
    result = (await client.get(path)).json()
    assert result["status"] == "processing" and result["clipId"]
    assert CONTEXT_KEY in job.snapshot


async def test_request_binding_and_account_scope(client, test_db_session, extractor):
    headers = await login(client, "admin")
    event, body, r = await start(client, test_db_session, headers)
    other = await alert(test_db_session, BASE + timedelta(seconds=5))
    for path, changed in [
        (f"{API}/{other}/exports", body),
        (f"{API}/{event}/exports", {**body, "beforeSeconds": 10}),
    ]:
        assert (await client.post(path, json=changed, headers=headers)).status_code == 409
    assert (await client.get(r.json()["statusPath"].replace(event, other))).status_code == 404
    await login(client, "operator")
    assert (await client.get(r.json()["statusPath"])).status_code == 404
    assert (await client.get(f"{API}/{event}/exports/{uuid4()}")).status_code == 404
    assert len(extractor["clips"]) == 1


async def test_failed_retry_lost_response_keeps_intent(client, test_db_session, extractor):
    headers = await login(client)
    event, body, r = await start(client, test_db_session, headers)
    clip = extractor["clips"][r.json()["clipId"]]
    clip.update(
        status="failed", error={"code": "arbitrary-private-secret", "message": "private-secret"}
    )
    failed = await client.get(r.json()["statusPath"])
    assert (
        failed.json()["errorCode"] == "EVENT_EXPORT_FAILED" and "private-secret" not in failed.text
    )
    duplicate = await client.post(f"{API}/{event}/exports", json=body, headers=headers)
    assert duplicate.json()["status"] == "failed"
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1
    extractor["lost"] = True
    retry = await client.post(
        f"{API}/{event}/exports", json={**body, "retryFailed": True}, headers=headers
    )
    assert retry.status_code == 504
    assert (await client.get(r.json()["statusPath"])).json()["status"] == "processing"
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    assert job.snapshot[CONTEXT_KEY]["retry_failed"] is True
    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await test_db_session.commit()
    await MediaJobWorker().run_once(test_db_session)
    assert len(extractor["clips"]) == 1 and clip["attempt_count"] == 2
    await test_db_session.refresh(job)
    assert job.snapshot[CONTEXT_KEY]["retry_failed"] is False


@pytest.mark.parametrize(
    "mismatch",
    [
        {"request_mode": None},
        {"attempt_count": None},
        {"requested_start_at": "2026-10-14T02:59:29.000Z"},
        {"camera_id": "other-camera"},
        {"event_id": "other-event"},
    ],
)
async def test_mismatched_receipt_is_not_downloadable(client, test_db_session, extractor, mismatch):
    headers = await login(client)
    extractor["mismatch"] = mismatch
    _, _, r = await start(client, test_db_session, headers)
    assert r.status_code == 502, r.text
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    assert job.clip_id is None and "clip_id" not in job.snapshot


async def test_changed_source_blocks_poll(client, test_db_session, extractor, monkeypatch):
    headers = await login(client)
    _, _, r = await start(client, test_db_session, headers)
    count = len(extractor["requests"])
    monkeypatch.setattr(get_settings(), "MEDIA_SERVICE_URL", "http://other.test/api/v1")
    response = await client.get(r.json()["statusPath"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MEDIA_CLIP_SOURCE_CHANGED"
    assert len(extractor["requests"]) == count


@pytest.mark.parametrize(
    "status,code",
    [
        (404, "RECORDING_NOT_FOUND_IN_RANGE"),
        (409, "IDEMPOTENCY_CONFLICT"),
        (409, "RETRY_NOT_ALLOWED"),
        (503, "SERVICE_BUSY"),
        (400, "REQUEST_TIME_IN_FUTURE"),
    ],
)
async def test_safe_upstream_errors(client, test_db_session, extractor, status, code):
    headers = await login(client)
    extractor["error"] = (status, code)
    _, _, r = await start(client, test_db_session, headers)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert "private-secret" not in r.text


async def test_gets_advertise_explicit_export_without_creating_files(
    client, test_db_session, extractor, monkeypatch
):
    from src.app.schemas.daily_recording import DailyPage
    from src.app.services.daily_recording_service import daily_recording_service

    async def empty_archive(params):
        return DailyPage(items=[], next_cursor=None)

    monkeypatch.setattr(daily_recording_service, "list", empty_archive)
    event = await alert(test_db_session)
    await login(client)
    assert (await client.get(API)).status_code == 200
    replay = await client.get(f"{API}/{event}")
    assert replay.status_code == 200, replay.text
    assert replay.json()["exportPath"] == f"{API}/{event}/exports"
    assert replay.json()["downloadReasonCode"] == "EVENT_EXPORT_NOT_REQUESTED"
    assert replay.json()["downloadPath"] is None
    assert extractor["requests"] == []
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_deleted_export_is_not_revived_and_legacy_delete_keeps_context(
    client, test_db_session, extractor
):
    headers = await login(client, "admin")
    event, body, response = await start(client, test_db_session, headers)
    item = response.json()
    extractor["clips"][item["clipId"]].update(status="deleted")
    response = await client.post(
        f"{API}/{event}/exports", json={**body, "retryFailed": True}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deleted" and response.json()["downloadPath"] is None
    assert len(extractor["clips"]) == 1
    jobs = (await client.get("/api/v1/media/clip-jobs")).json()
    assert jobs[0]["time_basis"] == "EVENT_RANGE"
    deleted = await client.delete(f"/api/v1/media/clips/{item['clipId']}", headers=headers)
    assert deleted.status_code == 204
    detail = await client.get(item["statusPath"])
    assert detail.status_code == 200 and detail.json()["status"] == "deleted"
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    assert CONTEXT_KEY in job.snapshot
    # Exports are not duplicate entries in the legacy recordings list/detail.
    legacy = await client.get("/api/v1/recordings", params={"pit_id": "pit-demo"})
    assert legacy.status_code == 200 and legacy.json()["items"] == []


async def test_new_request_id_can_request_another_file(client, test_db_session, extractor):
    headers = await login(client)
    event, _, first = await start(client, test_db_session, headers)
    second = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert second.status_code == 202 and first.json()["clipId"] != second.json()["clipId"]
    assert len(extractor["clips"]) == 2


async def test_invalid_stored_context_fails_without_http(client, test_db_session, extractor):
    headers = await login(client)
    _, _, first = await start(client, test_db_session, headers)
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    job.snapshot = {CONTEXT_KEY: {"version": 99}}
    await test_db_session.commit()
    count = len(extractor["requests"])
    response = await client.get(first.json()["statusPath"])
    assert response.status_code == 409
    assert len(extractor["requests"]) == count


@pytest.mark.parametrize("kind", ["manual", "error"])
async def test_other_event_categories_use_recorded_context(
    client, test_db_session, extractor, kind
):
    from src.app.models.edge_v2_projection import EdgeV2Transition
    from src.app.models.log import CollectionLog

    if kind == "manual":
        row = CollectionLog(
            pit_id="pit-demo", completed_at=BASE, residual_height_cm=0, operator_name="Synthetic"
        )
        prefix = "evt-col"
    else:
        row = EdgeV2Transition(
            site_id="site-demo",
            edge_id="edge-demo",
            kind="DEVICE_STATE_CHANGED",
            occurred_at=BASE,
            payload={
                "pitId": "pit-demo",
                "previous": "HEALTHY",
                "current": "MEASUREMENT_UNAVAILABLE",
            },
        )
        prefix = "evt-v2-transition"
    test_db_session.add(row)
    await test_db_session.commit()
    event_id = f"{prefix}-{row.id}"
    headers = await login(client)
    response = await client.post(
        f"{API}/{event_id}/exports",
        json=payload(beforeSeconds=10, afterSeconds=20),
        headers=headers,
    )
    assert response.status_code == 202, response.text
    body = json.loads(extractor["requests"][0].content)
    assert body["start_at"] == "2026-10-14T02:59:50.000Z"
    assert body["end_at"] == "2026-10-14T03:00:20.000Z"


async def test_first_retry_cannot_create_file(client, test_db_session, extractor):
    headers = await login(client)
    event = await alert(test_db_session)
    response = await client.post(
        f"{API}/{event}/exports", json=payload(retryFailed=True), headers=headers
    )
    assert response.status_code == 404
    assert extractor["requests"] == []
