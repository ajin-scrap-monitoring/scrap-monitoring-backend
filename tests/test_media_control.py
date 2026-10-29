"""Media v1 contract, authenticated controls, and durable event recording recovery."""

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import Settings, get_settings
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.services.alert_engine import AlertEngine
from src.app.services.media_client import MediaServiceError, media_client
from src.app.services.media_job_service import (
    MediaJobWorker,
    enqueue_event_clip,
    media_job_worker,
    process_job,
)

CLIP_ID = "550e8400-e29b-41d4-a716-446655440000"
NOW = datetime.now(UTC).replace(microsecond=0)


def utc(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeMedia:
    """Stateful fake: accepts the first request even when its response is lost."""

    def __init__(self):
        self.requests = []
        self.policy = {"pre_roll_seconds": 30, "post_roll_seconds": 30, "retention_days": 30}
        self.created = False
        self.lose_first_response = False
        self.status = "collecting"
        self.coverage = "complete"
        self.event_id = "evt-alt-1"
        self.error_status = None
        self.error_code = "STORAGE_UNAVAILABLE"

    def receipt(self):
        return {
            "clip_id": CLIP_ID,
            "event_id": self.event_id,
            "camera_id": "camera-main",
            "status": self.status,
            "accepted_at": utc(NOW),
            "requested_start_at": utc(NOW - timedelta(seconds=30)),
            "requested_end_at": utc(NOW + timedelta(seconds=30)),
        }

    def detail(self):
        result = self.receipt()
        finished = self.status in ("ready", "deleted", "expired")
        return {
            **result,
            "coverage": self.coverage if self.status not in ("collecting", "processing") else None,
            "recording_policy": self.policy,
            "actual_start_at": utc(NOW - timedelta(seconds=20)) if finished else None,
            "actual_end_at": utc(NOW + timedelta(seconds=30)) if finished else None,
            "gaps": [
                {
                    "start_at": utc(NOW - timedelta(seconds=30)),
                    "end_at": utc(NOW - timedelta(seconds=20)),
                }
            ]
            if self.coverage == "partial"
            else [],
            "completed_at": utc(NOW + timedelta(seconds=32)) if finished else None,
            "expires_at": utc(NOW + timedelta(days=30)) if finished else None,
            "deleted_at": utc(NOW) if self.status == "deleted" else None,
            "content_path": f"/api/v1/clips/{CLIP_ID}/content" if self.status == "ready" else None,
            "error": {"code": "NO_FRAMES_AVAILABLE", "message": "No frames"}
            if self.status == "failed"
            else None,
        }

    async def handle(self, request):
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-media-token"
        assert request.url.host == "media.test"
        if self.error_status:
            return httpx.Response(
                self.error_status,
                json={
                    "error": {
                        "code": self.error_code,
                        "message": "synthetic-media-token must not be exposed",
                    }
                },
            )
        if request.url.path.endswith("recording-policy"):
            if request.method == "PUT":
                self.policy = json.loads(request.content)
            return httpx.Response(200, json=self.policy)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert set(payload) == {"event_id", "camera_id"}
            self.event_id = payload["event_id"]
            was_created = self.created
            self.created = True
            if self.lose_first_response and not was_created:
                raise httpx.ReadTimeout("Response lost", request=request)
            return httpx.Response(200 if was_created else 202, json=self.receipt())
        if request.method == "DELETE":
            if self.status in ("collecting", "processing"):
                return httpx.Response(409, json={"error": {"code": "CLIP_IN_PROGRESS"}})
            if self.status != "expired":
                self.status = "deleted"
            return httpx.Response(204)
        if request.url.path == "/api/v1/clips":
            return httpx.Response(200, json={"items": [self.detail()], "next_cursor": "opaque+/="})
        assert request.url.path == f"/api/v1/clips/{CLIP_ID}"
        return httpx.Response(200, json=self.detail())


@pytest.fixture
async def fake_media(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://media.test/api/v1")
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", SecretStr("synthetic-media-token"))
    monkeypatch.setattr(media_job_worker, "lock", asyncio.Lock())
    fake = FakeMedia()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        yield fake


async def login(client, username="admin"):
    response = await client.post(
        "/api/v1/session",
        json={
            "username": username,
            "password": "admin1234" if username == "admin" else "operator1234",
        },
    )
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrfToken"]}


async def seed_alert(db):
    event = AlertLog(
        pit_id="pit-01",
        triggered_at=NOW,
        fill_ratio_percent=95,
        channel="LOG",
        status="SENT",
        message="Synthetic test alert",
    )
    db.add(event)
    await db.commit()
    return f"evt-alt-{event.id}"


async def due(db):
    job = await db.scalar(select(MediaClipJob))
    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    return job


async def test_policy_authorization_and_validation(client, fake_media):
    path = "/api/v1/media/cameras/camera-main/recording-policy"
    assert (await client.get(path)).status_code == 401
    viewer = await login(client, "operator")
    assert (await client.get(path)).json() == fake_media.policy
    assert (await client.put(path, json=fake_media.policy, headers=viewer)).status_code == 403
    headers = await login(client)
    assert (await client.put(path, json=fake_media.policy)).status_code == 403
    policy = {"pre_roll_seconds": 0, "post_roll_seconds": 60, "retention_days": 7}
    assert (await client.put(path, json=policy, headers=headers)).json() == policy
    for invalid in (
        {**policy, "post_roll_seconds": 0},
        {**policy, "retention_days": 366},
        {**policy, "pre_roll_seconds": True},
        {**policy, "extra": 1},
    ):
        assert (await client.put(path, json=invalid, headers=headers)).status_code == 422


async def test_clip_lifecycle_dashboard_and_history(client, test_db_session, fake_media):
    headers = await login(client)
    event_id = await seed_alert(test_db_session)
    request = {"event_id": event_id, "camera_id": "camera-main"}
    response = await client.post("/api/v1/media/clips", json=request, headers=headers)
    assert response.status_code == 202
    assert response.headers["location"] == f"/api/v1/media/clips/{CLIP_ID}"
    assert (await client.delete(response.headers["location"], headers=headers)).status_code == 409
    fake_media.status = "ready"
    fake_media.coverage = "partial"
    await due(test_db_session)
    await media_job_worker.run_once(test_db_session)
    assert (
        await client.post("/api/v1/media/clips", json=request, headers=headers)
    ).status_code == 200
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 1
    page = (await client.get("/api/v1/recordings?category=alert")).json()
    assert page["page"]["totalItems"] == 1
    recording = page["items"][0]
    assert recording["id"] == CLIP_ID
    assert recording["status"] == "available"
    assert recording["coverage"] == "partial" and len(recording["gaps"]) == 1
    assert recording["sizeBytes"] is None and recording["widthPixels"] is None
    assert recording["contentPath"] == f"/media/clips/{CLIP_ID}/content"
    assert recording["downloadPath"].endswith("?download=true")
    events = (await client.get("/api/v1/events")).json()["items"]
    assert events[0]["recordingId"] == CLIP_ID
    deleted = await client.delete(response.headers["location"], headers=headers)
    assert deleted.status_code == 204 and not deleted.content
    # Hide stale file links immediately, before the worker refreshes remote status.
    page = (await client.get("/api/v1/recordings")).json()
    assert page["items"][0]["contentPath"] is None
    await media_job_worker.run_once(test_db_session)
    detail = (await client.get(f"/api/v1/recordings/{CLIP_ID}")).json()
    assert detail["mediaStatus"] == "deleted" and detail["contentPath"] is None


async def test_lost_ack_and_worker_restart_reuse_identity(client, test_db_session, fake_media):
    headers = await login(client)
    event_id = await seed_alert(test_db_session)
    fake_media.lose_first_response = True
    response = await client.post(
        "/api/v1/media/clips", headers=headers, json={"event_id": event_id}
    )
    assert response.status_code == 504
    job = await due(test_db_session)
    assert job.clip_id is None and job.attempts == 1
    # A fresh worker recovers the existing row rather than minting another event.
    restarted_worker = MediaJobWorker()
    await restarted_worker.run_once(test_db_session)
    assert job.clip_id == CLIP_ID and job.attempts == 2
    posts = [
        json.loads(request.content) for request in fake_media.requests if request.method == "POST"
    ]
    assert posts == [{"event_id": event_id, "camera_id": "camera-main"}] * 2
    fake_media.status = "failed"
    fake_media.coverage = "none"
    await due(test_db_session)
    await restarted_worker.run_once(test_db_session)
    assert job.status == "failed" and job.next_attempt_at is None
    jobs = (await client.get("/api/v1/media/clip-jobs")).json()
    assert jobs[0]["clip"]["error"]["code"] == "NO_FRAMES_AVAILABLE"


async def test_collection_queues_without_upstream_io(client, test_db_session, fake_media):
    headers = await login(client)
    response = await client.post(
        "/api/v1/collections/confirm",
        json={
            "pit_id": "pit-01",
            "residual_height_cm": 10,
            "operator_name": "Synthetic operator",
        },
        headers=headers,
    )
    assert response.status_code == 201
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.event_id == f"evt-col-{response.json()['id']}"
    assert job.category == "collection" and job.clip_id is None
    assert not fake_media.requests
    assert await enqueue_event_clip(test_db_session, "other", "pit-other", "alert", NOW) is None


async def test_remote_cursor_filters_and_time_validation(client, fake_media):
    await login(client)
    response = await client.get(
        "/api/v1/media/clips",
        params={
            "camera_id": "camera-main",
            "event_id": "evt-alt-1",
            "cursor": "opaque+/=",
            "from": utc(NOW - timedelta(days=1)),
            "to": utc(NOW),
            "status": "ready",
            "limit": 7,
        },
    )
    assert response.status_code == 200 and response.json()["next_cursor"] == "opaque+/="
    assert fake_media.requests[-1].url.params["cursor"] == "opaque+/="
    for query in (
        {"from": utc(NOW), "to": utc(NOW)},
        {"from": "2026-01-01T00:00:00"},
        {"limit": 101},
    ):
        assert (await client.get("/api/v1/media/clips", params=query)).status_code == 422


@pytest.mark.parametrize(
    "http_status,public_status,code",
    [
        (401, 502, "MEDIA_AUTH_FAILED"),
        (503, 503, "STORAGE_UNAVAILABLE"),
        (404, 404, "CLIP_NOT_FOUND"),
        (409, 409, "CLIP_NOT_READY"),
        (410, 410, "CLIP_EXPIRED"),
        (302, 502, "MEDIA_BAD_RESPONSE"),
    ],
)
async def test_remote_errors_do_not_leak_tokens(
    client, fake_media, http_status, public_status, code
):
    await login(client)
    fake_media.error_status = http_status
    fake_media.error_code = code
    response = await client.get(f"/api/v1/media/clips/{CLIP_ID}")
    assert response.status_code == public_status
    assert response.json()["error"]["code"] == code
    assert "synthetic-media-token" not in response.text


async def test_media_access_and_legacy_callback_guard(client, fake_media):
    assert (await client.get("/api/v1/media/access")).status_code == 401
    assert (await client.get("/api/v1/recordings")).status_code == 401
    await login(client, "operator")
    assert (await client.get("/api/v1/media/access")).status_code == 204
    verify = (await client.get("/api/v1/media/session/verify")).json()
    assert verify["stream_url"] == "/media/cameras/camera-main/live"
    # FastAPI must not expose live streams or file bodies itself.
    assert (await client.get(f"/api/v1/media/clips/{CLIP_ID}/content")).status_code == 404
    assert (await client.get("/api/v1/media/cameras/camera-main/live")).status_code == 404


async def test_bad_identity_and_malicious_content_path_are_rejected(test_db_session, fake_media):
    job = await enqueue_event_clip(test_db_session, "evt-alt-1", "pit-01", "alert", NOW)
    await test_db_session.commit()
    await process_job(job, test_db_session)
    fake_media.event_id = "evt-alt-unrelated"
    with pytest.raises(MediaServiceError, match="MEDIA_IDENTITY_MISMATCH"):
        await process_job(job, test_db_session)
    assert job.error_code == "MEDIA_IDENTITY_MISMATCH" and job.next_attempt_at is None
    fake_media.event_id = "evt-alt-1"
    fake_media.status = "ready"
    original_detail = fake_media.detail
    fake_media.detail = lambda: {**original_detail(), "content_path": "https://untrusted.test/file"}
    with pytest.raises(MediaServiceError, match="MEDIA_BAD_RESPONSE"):
        await media_client.get_clip(UUID(CLIP_ID))


def test_media_settings_validate_pairs_and_hide_secret():
    settings = Settings(
        _env_file=None,
        MEDIA_SERVICE_URL="http://media.test/api/v1",
        MEDIA_API_TOKEN="synthetic-media-token",
    )
    assert "synthetic-media-token" not in repr(settings)
    for overrides in (
        {"MEDIA_SERVICE_URL": "http://media.test/api/v1"},
        {"MEDIA_API_TOKEN": "synthetic-media-token"},
        {
            "MEDIA_SERVICE_URL": "http://user:password@media.test/api/v1",
            "MEDIA_API_TOKEN": "synthetic-media-token",
        },
        {
            "MEDIA_SERVICE_URL": "http://media.test/api/v1?token=secret",
            "MEDIA_API_TOKEN": "synthetic-media-token",
        },
        {
            "MEDIA_SERVICE_URL": "http://media.test:0/api/v1",
            "MEDIA_API_TOKEN": "synthetic-media-token",
        },
        {"MEDIA_SERVICE_URL": "http://media.test/api/v1", "MEDIA_API_TOKEN": 'invalid"token'},
        {"MEDIA_CAMERA_ID": "c" * 129},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **overrides)


async def test_alert_and_worker_lifecycle_use_persisted_jobs(
    test_engine, test_db_session, fake_media, monkeypatch
):
    alert_module = importlib.import_module("src.app.services.alert_engine")
    job_module = importlib.import_module("src.app.services.media_job_service")

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(alert_module, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(job_module, "get_sessionmaker", lambda: factory)
    engine = AlertEngine()
    await engine._process_alert("pit-01", 95.0, True)
    await engine._process_alert("pit-01", 96.0, False)
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 1
    assert not fake_media.requests
    worker = MediaJobWorker()
    finished = asyncio.Event()
    run_once = worker.run_once

    async def observe_sweep(db):
        await run_once(db)
        finished.set()

    monkeypatch.setattr(worker, "run_once", observe_sweep)
    worker.start()
    try:
        await asyncio.wait_for(finished.wait(), timeout=2)
        test_db_session.expire_all()
        job = await test_db_session.scalar(select(MediaClipJob))
        assert job.clip_id == CLIP_ID and job.status == "collecting"
    finally:
        await worker.stop()
    assert worker._task is None


async def test_missing_event_auth_failure_and_terminal_expiry(client, test_db_session, fake_media):
    headers = await login(client)
    request = {"event_id": "evt-alt-999"}
    assert (
        await client.post("/api/v1/media/clips", json=request, headers=headers)
    ).status_code == 404
    for event_alias in ("evt-alt-01", "evt-alt-" + "9" * 100, "evt-alt-٠١"):
        response = await client.post(
            "/api/v1/media/clips", json={"event_id": event_alias}, headers=headers
        )
        assert response.status_code == 404
    event_id = await seed_alert(test_db_session)
    request["event_id"] = event_id
    fake_media.error_status = 401
    response = await client.post("/api/v1/media/clips", json=request, headers=headers)
    assert response.status_code == 502
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.status == "blocked" and job.next_attempt_at is None
    fake_media.error_status = None
    assert (
        await client.post("/api/v1/media/clips", json=request, headers=headers)
    ).status_code == 202
    fake_media.status = "expired"
    await due(test_db_session)
    await media_job_worker.run_once(test_db_session)
    assert job.status == "expired" and job.next_attempt_at is None
    # An idempotent delete must preserve expired rather than invent a deleted state.
    assert (
        await client.delete(f"/api/v1/media/clips/{CLIP_ID}", headers=headers)
    ).status_code == 204
    await media_job_worker.run_once(test_db_session)
    assert job.status == "expired"


async def test_legacy_callback_disabled_and_manual_clip_permissions(client, fake_media):
    payload = {
        "segment_id": "synthetic-legacy",
        "pit_id": "pit-01",
        "start_time": utc(NOW),
        "end_time": utc(NOW + timedelta(seconds=60)),
        "file_path": "/synthetic/legacy.mp4",
        "file_size_bytes": 1,
        "duration_seconds": 60,
    }
    assert (await client.post("/api/v1/media/recordings/complete", json=payload)).status_code == 410
    headers = await login(client, "operator")
    assert (
        await client.post("/api/v1/media/clips", json={"event_id": "evt-alt-1"}, headers=headers)
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/collections/confirm",
            json={
                "pit_id": "pit-01",
                "residual_height_cm": 10,
                "operator_name": "Synthetic operator",
            },
            headers=headers,
        )
    ).status_code == 403


@pytest.mark.parametrize("prefix", ["evt-alt", "evt-col"])
@pytest.mark.parametrize("use_event_time", [False, True])
@pytest.mark.parametrize("number", [2147483648, 9223372036854775807])
async def test_clip_event_number_outside_database_range_is_not_found(
    client, test_db_session, fake_media, prefix, use_event_time, number
):
    response = await client.post(
        "/api/v1/media/clips",
        json={"event_id": f"{prefix}-{number}", "use_event_time": use_event_time},
        headers=await login(client),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "EVENT_NOT_FOUND"
    assert fake_media.requests == []
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("prefix", ["evt-alt", "evt-col"])
async def test_clip_accepts_largest_database_event_number(
    client, test_db_session, fake_media, prefix
):
    if prefix == "evt-alt":
        await seed_alert(test_db_session)
        event = await test_db_session.scalar(select(AlertLog))
    else:
        event = CollectionLog(
            pit_id="pit-01", completed_at=NOW, residual_height_cm=0, operator_name="Synthetic"
        )
        test_db_session.add(event)
    event.id = 2147483647
    await test_db_session.commit()
    response = await client.post(
        "/api/v1/media/clips",
        json={"event_id": f"{prefix}-{event.id}"},
        headers=await login(client),
    )
    assert response.status_code == 202
    assert response.json()["event_id"] == f"{prefix}-2147483647"
    assert len(fake_media.requests) == 1
