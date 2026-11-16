"""Acceptance remains independent of slow upstream work and its I/O lock."""

import asyncio
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_daily_recordings import login
from test_event_exports import API, extractor, payload, start
from test_event_recordings import BASE, alert

from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.models.media_job import EventExportAlias, MediaClipJob
from src.app.services.media_client import media_client
from src.app.services.media_job_service import MediaJobWorker, media_job_worker

__all__ = ["extractor"]


async def test_eight_second_media_delay_does_not_delay_acceptance_or_replays(
    client, test_db_session, test_engine, extractor, monkeypatch
):
    from datetime import timedelta

    # Let the synthetic media really take eight seconds; the normal 3s test
    # timeout would otherwise terminate the upstream call before this scenario.
    monkeypatch.setattr(get_settings(), "MEDIA_REQUEST_TIMEOUT_SECONDS", 12)
    headers = await login(client)
    event = await alert(test_db_session)
    other = await alert(test_db_session, BASE + timedelta(seconds=5))
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def independent_session():
        async with factory() as db:
            yield db

    client._transport.app.dependency_overrides[get_db_session] = independent_session
    entered = asyncio.Event()
    transport = media_client._client._transport
    original = transport.handle_async_request
    delayed_once = False

    async def slow_media(request):
        nonlocal delayed_once
        if request.method == "POST" and not delayed_once:
            delayed_once = True
            entered.set()
            await asyncio.sleep(8.1)
        return await original(request)

    monkeypatch.setattr(transport, "handle_async_request", slow_media)
    body = payload()
    began = time.monotonic()
    first = await asyncio.wait_for(
        client.post(f"{API}/{event}/exports", json=body, headers=headers), 2
    )
    first_elapsed = time.monotonic() - began
    assert first_elapsed < 2
    assert first.status_code == 202 and first.json()["status"] == "pending"
    assert first.headers["location"] == first.json()["statusPath"]
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["clipId"] is None and extractor["requests"] == []

    async def run_worker():
        async with factory() as db:
            await media_job_worker.run_once(db)

    started = time.monotonic()
    worker = asyncio.create_task(run_worker())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert media_job_worker.lock.locked()
        began = time.monotonic()
        for target, request_body, expected in [
            (event, body, 200),
            (event, payload(), 200),
            (other, payload(), 202),
        ]:
            response = await asyncio.wait_for(
                client.post(f"{API}/{target}/exports", json=request_body, headers=headers), 2
            )
            assert response.status_code == expected, response.text
            assert response.json()["clipId"] is None
        concurrent_elapsed = time.monotonic() - began
        assert concurrent_elapsed < 2
        assert not worker.done() and extractor["requests"] == []
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 2
            assert await db.scalar(select(func.count()).select_from(EventExportAlias)) == 3
        await asyncio.wait_for(asyncio.shield(worker), 15)
        worker_elapsed = time.monotonic() - started
        assert worker_elapsed >= 8
        print(
            f"Q20 delay: worker={worker_elapsed:.3f}s, first_ack={first_elapsed:.3f}s, "
            f"three_concurrent_acks={concurrent_elapsed:.3f}s"
        )
        # A fresh worker instance discovers the other durable pending job.
        async with factory() as db:
            await MediaJobWorker().run_once(db)
        status = await client.get(first.json()["statusPath"])
        assert status.status_code == 200 and status.json()["status"] == "processing"
        assert status.json()["clipId"] and len(extractor["clips"]) == 2
        assert sum(r.method == "POST" for r in extractor["requests"]) == 2
    finally:
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker


async def test_acceptance_survives_new_worker_without_media_call_in_post(
    client, test_db_session, extractor
):
    headers = await login(client)
    event, body = await alert(test_db_session), payload()
    response = await client.post(f"{API}/{event}/exports", json=body, headers=headers)
    assert response.status_code == 202 and extractor["requests"] == []
    persisted = await test_db_session.scalar(select(MediaClipJob))
    assert persisted.attempts == 0 and persisted.next_attempt_at is not None
    assert (await client.get(response.json()["statusPath"])).json()["status"] == "pending"
    assert extractor["requests"] == []
    await MediaJobWorker().run_once(test_db_session)
    after = await client.get(response.json()["statusPath"])
    assert after.json()["status"] == "processing" and after.json()["clipId"]
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1


@pytest.mark.parametrize(
    "error", [(409, "RETRY_NOT_ALLOWED"), (404, "RECORDING_NOT_FOUND_IN_RANGE")]
)
async def test_permanent_retry_failure_is_not_revived_by_get_or_duplicate_post(
    client, test_db_session, extractor, error
):
    headers = await login(client)
    event, body, first = await start(client, test_db_session, headers)
    clip = extractor["clips"][first.json()["clipId"]]
    clip.update(status="failed", error={"code": "EXPORT_FAILED", "message": "private-secret"})
    path = first.json()["statusPath"]
    assert (await client.get(path)).json()["status"] == "failed"
    retry = await client.post(
        f"{API}/{event}/exports", json={**body, "retryFailed": True}, headers=headers
    )
    assert retry.status_code == 202 and retry.json()["status"] == "pending"
    extractor["error"] = error
    await MediaJobWorker().run_once(test_db_session)
    saved = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(saved)
    assert saved.status == "blocked" and saved.next_attempt_at is None
    assert saved.snapshot["_rangeExport"]["retry_failed"] is False
    duplicate = await client.post(f"{API}/{event}/exports", json=body, headers=headers)
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "blocked"
    seen = sum(r.method == "POST" for r in extractor["requests"])
    extractor["error"] = None
    # A GET may observe the existing remote failure but never schedules its retry.
    assert (await client.get(path)).json()["status"] == "failed"
    await MediaJobWorker().run_once(test_db_session)
    assert sum(r.method == "POST" for r in extractor["requests"]) == seen
    assert clip["attempt_count"] == 1


@pytest.mark.parametrize("reader", ["status", "worker"])
async def test_retry_accepted_during_old_get_survives_its_late_snapshot(
    client, test_db_session, test_engine, extractor, monkeypatch, reader
):
    from datetime import UTC, datetime

    headers = await login(client)
    event, body, first = await start(client, test_db_session, headers)
    clip = extractor["clips"][first.json()["clipId"]]
    clip.update(status="failed", error={"code": "EXPORT_FAILED", "message": "private-secret"})
    assert (await client.get(first.json()["statusPath"])).json()["status"] == "failed"
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    if reader == "worker":
        job.next_attempt_at = datetime.now(UTC)
        await test_db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def independent_session():
        async with factory() as db:
            yield db

    client._transport.app.dependency_overrides[get_db_session] = independent_session
    entered, release = asyncio.Event(), asyncio.Event()
    transport = media_client._client._transport
    original = transport.handle_async_request

    async def old_response(request):
        response = await original(request)
        if request.method == "GET":
            entered.set()
            await release.wait()
        return response

    monkeypatch.setattr(transport, "handle_async_request", old_response)

    async def run_reader():
        if reader == "status":
            response = await client.get(first.json()["statusPath"])
            assert response.status_code == 200 and response.json()["status"] == "pending"
        else:
            async with factory() as db:
                await media_job_worker.run_once(db)

    task = asyncio.create_task(run_reader())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        retry = await asyncio.wait_for(
            client.post(
                f"{API}/{event}/exports", json={**body, "retryFailed": True}, headers=headers
            ),
            2,
        )
        assert retry.status_code == 202 and retry.json()["status"] == "pending"
        assert retry.json()["downloadPath"] is None
        release.set()
        await asyncio.wait_for(task, 2)
        async with factory() as db:
            saved = await db.get(MediaClipJob, job.id)
            assert saved.status == "pending" and saved.snapshot["_rangeExport"]["retry_failed"]
            assert saved.next_attempt_at is not None
            await MediaJobWorker().run_once(db)
        status = await client.get(first.json()["statusPath"])
        assert status.json()["status"] == "processing"
        assert status.json()["clipId"] == first.json()["clipId"]
        assert clip["attempt_count"] == 2 and len(extractor["clips"]) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
