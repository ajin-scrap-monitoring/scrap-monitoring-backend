"""Lost DELETE acknowledgements are reconciled by reads, never another mutation."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from test_event_exports import extractor as extractor
from test_media_control import CLIP_ID, NOW, due, login, seed_alert, utc
from test_media_control import fake_media as fake_media

from src.app.core.config import get_settings
from src.app.models.media_job import MediaClipJob
from src.app.services.media_client import media_client
from src.app.services.media_job_service import MediaJobWorker, event_time_source, media_job_worker

PATH = f"/api/v1/media/clips/{CLIP_ID}"


@pytest.fixture
async def ready_clip(client, test_db_session, fake_media):
    headers = await login(client)
    event_id = await seed_alert(test_db_session)
    fake_media.status = "ready"
    response = await client.post(
        "/api/v1/media/clips", json={"event_id": event_id}, headers=headers
    )
    assert response.status_code == 202
    job = await due(test_db_session)
    await media_job_worker.run_once(test_db_session)
    assert job.status == "ready" and job.next_attempt_at is None
    return headers, job


@pytest.mark.parametrize("removed_state", ["deleted", "expired"])
@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_lost_delete_reply_confirms_real_removed_state(
    client, test_db_session, fake_media, ready_clip, monkeypatch, removed_state, failure_type
):
    headers, job = ready_clip
    methods = []

    async def handle(request):
        methods.append(request.method)
        if request.method == "DELETE":
            fake_media.status = removed_state
            raise failure_type("Lost response with synthetic-secret", request=request)
        return await fake_media.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        response = await client.delete(PATH, headers=headers)
    assert response.status_code == 204 and response.content == b""
    await test_db_session.refresh(job)
    assert job.status == removed_state and job.next_attempt_at is None
    assert job.snapshot["status"] == removed_state and job.snapshot["content_path"] is None
    assert methods == ["DELETE", "GET"]


@pytest.mark.parametrize(
    "verification", ["ready", "timeout", "not_found", "wrong_event", "wrong_camera", "slow"]
)
async def test_uncertain_delete_recovers_by_worker_get_without_repeating_delete(
    client, test_db_session, fake_media, ready_clip, monkeypatch, verification
):
    headers, job = ready_clip
    methods = []
    if verification == "slow":
        monkeypatch.setattr(get_settings(), "MEDIA_REQUEST_TIMEOUT_SECONDS", 0.03)

    async def handle(request):
        methods.append(request.method)
        if request.method == "DELETE" or verification == "timeout":
            raise httpx.ReadTimeout("Lost response with synthetic-secret", request=request)
        if verification == "not_found":
            return httpx.Response(404, json={"error": {"code": "CLIP_NOT_FOUND"}})
        if verification == "slow":
            await asyncio.sleep(1)
        response = await fake_media.handle(request)
        if verification == "wrong_event":
            return httpx.Response(
                200,
                json={
                    **response.json(),
                    "event_id": "other-event",
                    "status": "deleted",
                    "content_path": None,
                },
            )
        if verification == "wrong_camera":
            return httpx.Response(
                200,
                json={
                    **response.json(),
                    "camera_id": "other-camera",
                    "status": "deleted",
                    "content_path": None,
                },
            )
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        response = await client.delete(PATH, headers=headers)
        assert response.status_code == 504 and "synthetic-secret" not in response.text
        assert response.json()["error"]["code"] == "MEDIA_TIMEOUT"
        await test_db_session.refresh(job)
        assert job.status == "refreshing" and job.next_attempt_at is not None
        assert job.error_code == "MEDIA_TIMEOUT"
        page = (await client.get("/api/v1/recordings")).json()
        assert page["items"][0]["contentPath"] is None
        assert methods == ["DELETE", "GET"]
        # A new worker after the API response still reconciles the same clip by GET.
        fake_media.status = "deleted"
        monkeypatch.setattr(upstream, "_transport", httpx.MockTransport(fake_media.handle))
        job.next_attempt_at = datetime.now(UTC)
        await test_db_session.commit()
        await MediaJobWorker().run_once(test_db_session)
    await test_db_session.refresh(job)
    assert job.status == "deleted" and job.error_code is None and job.next_attempt_at is None


@pytest.mark.parametrize("removed_state", ["deleted", "expired"])
async def test_confirmed_removal_without_local_job_does_not_create_one(
    client, test_db_session, fake_media, monkeypatch, removed_state
):
    headers = await login(client)
    methods = []

    async def handle(request):
        methods.append(request.method)
        if request.method == "DELETE":
            fake_media.status = removed_state
            raise httpx.ReadTimeout("Lost acknowledgement", request=request)
        return await fake_media.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        assert (await client.delete(PATH, headers=headers)).status_code == 204
    assert await test_db_session.scalar(select(MediaClipJob)) is None
    assert methods == ["DELETE", "GET"]


@pytest.mark.parametrize(
    "http_status,code", [(409, "CLIP_IN_PROGRESS"), (503, "STORAGE_UNAVAILABLE")]
)
async def test_explicit_delete_rejection_keeps_snapshot_without_recovery(
    client, test_db_session, fake_media, ready_clip, http_status, code
):
    headers, job = ready_clip
    before = job.snapshot.copy()
    start = len(fake_media.requests)
    fake_media.error_status, fake_media.error_code = http_status, code
    response = await client.delete(PATH, headers=headers)
    assert response.status_code == http_status
    await test_db_session.refresh(job)
    assert job.status == "ready" and job.snapshot == before and job.next_attempt_at is None
    assert [r.method for r in fake_media.requests[start:]] == ["DELETE"]


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_delete_reconciliation_preserves_recorded_event_time(
    client, test_db_session, fake_media, ready_clip, monkeypatch, lost_reply
):
    headers, job = ready_clip
    context = {"version": 1, "eventAt": utc(NOW), "sourceToken": event_time_source()}
    job.snapshot = {**job.snapshot, "event_at": utc(NOW), "_eventTimeRequest": context}
    await test_db_session.commit()
    original_receipt = fake_media.receipt
    monkeypatch.setattr(fake_media, "receipt", lambda: {**original_receipt(), "event_at": utc(NOW)})
    methods = []

    async def handle(request):
        methods.append(request.method)
        if request.method == "DELETE" and lost_reply:
            fake_media.status = "deleted"
            raise httpx.ReadTimeout("Lost deletion response", request=request)
        return await fake_media.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        assert (await client.delete(PATH, headers=headers)).status_code == 204
        if not lost_reply:
            assert job.snapshot == {"_eventTimeRequest": context}
            await MediaJobWorker().run_once(test_db_session)
    assert job.status == "deleted" and job.snapshot["_eventTimeRequest"] == context
    assert methods == ["DELETE", "GET"]


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_delete_discards_extraction_retry_intent_without_losing_range(
    client, test_db_session, extractor, monkeypatch, lost_reply
):
    from test_event_export_reuse import ready
    from test_event_exports import start

    from src.app.services.event_export_service import CONTEXT_KEY

    headers = await login(client)
    _, _, response = await start(client, test_db_session, headers)
    item = response.json()
    clip = extractor["clips"][item["clipId"]]
    ready(clip)
    assert (await client.get(item["statusPath"])).json()["status"] == "ready"
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    context = {**job.snapshot[CONTEXT_KEY], "retry_failed": True}
    job.snapshot = {**job.snapshot, CONTEXT_KEY: context}
    await test_db_session.commit()
    upstream = media_client._client
    original = upstream._transport
    methods = []

    async def handle(request):
        methods.append(request.method)
        if lost_reply:
            raise httpx.ReadTimeout("Unconfirmed response", request=request)
        return await original.handle_async_request(request)

    monkeypatch.setattr(upstream, "_transport", httpx.MockTransport(handle))
    deleted = await client.delete(f"/api/v1/media/clips/{item['clipId']}", headers=headers)
    assert deleted.status_code == (504 if lost_reply else 204)
    await test_db_session.refresh(job)
    assert job.snapshot[CONTEXT_KEY] == {**context, "retry_failed": False}
    # Restarting the worker must read the same range instead of creating another file.
    clip.update(status="deleted", content_path=None)
    monkeypatch.setattr(upstream, "_transport", original)
    await MediaJobWorker().run_once(test_db_session)
    await test_db_session.refresh(job)
    assert job.status == "deleted" and job.next_attempt_at is None
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1
