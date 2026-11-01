"""Cross-browser recovery, durable aliases and scoped reuse of explicit downloads."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_daily_recordings import login
from test_event_exports import API, extractor, payload, start
from test_event_recordings import BASE, alert

from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.models.media_job import EventExportAlias, MediaClipJob
from src.app.services.media_job_service import MediaJobWorker

# Reuse the full media contract fixture, including failures and response-loss behavior.
__all__ = ["extractor"]


def ready(clip):
    clip.update(
        status="ready",
        content_path=f"/api/v1/clips/{clip['clip_id']}/content",
        completed_at=datetime.now(UTC).isoformat(),
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        size_bytes=4096,
        extraction_mode="stream_copy",
        available_intervals=[
            {"start_at": clip["requested_start_at"], "end_at": clip["requested_end_at"]}
        ],
    )


async def test_lookup_permissions_and_no_work(client, test_db_session, extractor):
    event = await alert(test_db_session)
    url = f"{API}/{event}/exports"
    assert (await client.get(url)).status_code == 401
    await login(client, "operator")
    result = await client.get(url)
    assert result.status_code == 200 and result.json() is None
    assert result.headers["cache-control"] == "no-store"
    assert (await client.get(f"{API}/evt-alt-2147483648/exports")).status_code == 404
    for params in [{"before_seconds": 301}, {"after_seconds": -1}]:
        assert (await client.get(url, params=params)).status_code == 422
    assert extractor["requests"] == []
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 0


@pytest.mark.parametrize("status", ["processing", "ready"])
async def test_other_browser_recovers_and_new_uuid_reuses(
    client, test_db_session, extractor, status
):
    headers = await login(client)
    event, first_body, first = await start(client, test_db_session, headers)
    original = first.json()
    if status == "ready":
        ready(extractor["clips"][original["clipId"]])
    # New login, no browser-side request storage.
    headers = await login(client)
    found = await client.get(f"{API}/{event}/exports")
    assert found.status_code == 200 and found.json()["requestId"] == first_body["requestId"]
    assert found.json()["status"] == status
    alias = payload()
    second = await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    assert second.status_code == 200 and second.json() == found.json()
    by_alias = await client.get(f"{API}/{event}/exports/{alias['requestId']}")
    assert by_alias.status_code == 200 and by_alias.json() == found.json()
    assert len(extractor["clips"]) == 1
    assert sum(r.method == "POST" for r in extractor["requests"]) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 2


async def test_alias_context_and_owner_are_bound(client, test_db_session, extractor):
    headers = await login(client, "admin")
    event, _, first = await start(client, test_db_session, headers)
    alias = payload()
    await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    other = await alert(test_db_session, BASE + timedelta(seconds=5))
    for target, body in [(other, alias), (event, {**alias, "beforeSeconds": 10})]:
        result = await client.post(f"{API}/{target}/exports", json=body, headers=headers)
        assert result.status_code == 409
    assert (await client.get(f"{API}/{other}/exports/{alias['requestId']}")).status_code == 404
    assert (await client.get(f"{API}/{event}/exports?before_seconds=10")).json() is None
    viewer = await login(client, "operator")
    assert (await client.get(f"{API}/{event}/exports")).json() is None
    assert (await client.get(f"{API}/{event}/exports/{alias['requestId']}")).status_code == 404
    separate = await client.post(f"{API}/{event}/exports", json=alias, headers=viewer)
    assert separate.status_code == 202 and separate.json()["clipId"] != first.json()["clipId"]


@pytest.mark.parametrize("status", ["deleted", "expired", "failed"])
async def test_alias_replay_is_not_rebound_to_new_file(client, test_db_session, extractor, status):
    headers = await login(client)
    event, _, first = await start(client, test_db_session, headers)
    alias = payload()
    await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    clip = extractor["clips"][first.json()["clipId"]]
    clip.update(status=status)
    old = await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    assert old.status_code == 200 and old.json()["status"] == status
    assert old.json()["downloadPath"] is None
    new = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert new.status_code == 202 and new.json()["clipId"] != old.json()["clipId"]
    replay = await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    assert replay.json()["requestId"] == first.json()["requestId"]
    assert replay.json()["status"] == status and len(extractor["clips"]) == 2


async def test_lost_create_response_is_recovered_by_lookup_without_post(
    client, test_db_session, extractor
):
    headers = await login(client)
    extractor["lost"] = True
    event, _, first = await start(client, test_db_session, headers)
    assert first.status_code == 504
    seen = len(extractor["requests"])
    found = await client.get(f"{API}/{event}/exports")
    assert found.json()["status"] == "pending" and found.json()["clipId"] is None
    assert len(extractor["requests"]) == seen
    again = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert again.status_code == 200 and again.json()["requestId"] == found.json()["requestId"]
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await test_db_session.commit()
    await MediaJobWorker().run_once(test_db_session)
    recovered = await client.get(found.json()["statusPath"])
    assert recovered.json()["status"] == "processing"
    assert len(extractor["clips"]) == 1


async def test_concurrent_new_ids_share_one_job(client, test_db_session, test_engine, extractor):
    headers = await login(client)
    event = await alert(test_db_session)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def independent_session():
        async with factory() as session:
            yield session

    client._transport.app.dependency_overrides[get_db_session] = independent_session
    results = await asyncio.gather(
        *(client.post(f"{API}/{event}/exports", json=payload(), headers=headers) for _ in range(6))
    )
    assert sorted(r.status_code for r in results) == [200] * 5 + [202]
    assert len({r.json()["requestId"] for r in results}) == 1
    assert len(extractor["clips"]) == 1


async def test_existing_jobs_without_aliases_remain_discoverable(
    client, test_db_session, extractor
):
    from sqlalchemy import delete

    headers = await login(client)
    event, first_body, first = await start(client, test_db_session, headers)
    await test_db_session.execute(delete(EventExportAlias))
    await test_db_session.commit()
    found = await client.get(f"{API}/{event}/exports")
    assert found.json()["requestId"] == first_body["requestId"]
    assert (await client.get(first.json()["statusPath"])).status_code == 200
    reused = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert reused.status_code == 200 and reused.json()["clipId"] == first.json()["clipId"]


async def test_expired_ready_file_is_not_reused(client, test_db_session, extractor):
    headers = await login(client)
    event, _, first = await start(client, test_db_session, headers)
    clip = extractor["clips"][first.json()["clipId"]]
    ready(clip)
    clip["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await client.get(first.json()["statusPath"])
    second = await client.post(f"{API}/{event}/exports", json=payload(), headers=headers)
    assert second.status_code == 202 and second.json()["clipId"] != first.json()["clipId"]


async def test_context_change_get_and_post_are_separate(client, test_db_session, extractor):
    headers = await login(client)
    event, _, first = await start(client, test_db_session, headers)
    second = await client.post(
        f"{API}/{event}/exports", json=payload(beforeSeconds=5, afterSeconds=10), headers=headers
    )
    assert second.status_code == 202
    assert second.json()["clipId"] != first.json()["clipId"]
    found = await client.get(f"{API}/{event}/exports?before_seconds=5&after_seconds=10")
    assert found.json()["requestId"] == second.json()["requestId"]
    default = await client.get(f"{API}/{event}/exports")
    assert default.json()["requestId"] == first.json()["requestId"]


async def test_changed_source_is_not_reused(client, test_db_session, extractor, monkeypatch):
    headers = await login(client)
    event, original, _ = await start(client, test_db_session, headers)
    monkeypatch.setattr(get_settings(), "MEDIA_SERVICE_URL", "http://other.test/api/v1")
    count = len(extractor["requests"])
    assert (await client.get(f"{API}/{event}/exports")).json() is None
    old = await client.post(f"{API}/{event}/exports", json=original, headers=headers)
    assert old.status_code == 409 and old.json()["error"]["code"] == "MEDIA_CLIP_SOURCE_CHANGED"
    assert len(extractor["requests"]) == count


async def test_refresh_failure_does_not_create_replacement(client, test_db_session, extractor):
    headers = await login(client)
    event, _, _ = await start(client, test_db_session, headers)
    extractor["error"] = (503, "SERVICE_BUSY")
    for method in ["GET", "POST"]:
        result = await client.request(
            method,
            f"{API}/{event}/exports",
            **({"json": payload(), "headers": headers} if method == "POST" else {}),
        )
        assert result.status_code == 503 and "private-secret" not in result.text
    assert len(extractor["clips"]) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 1


async def test_alias_failed_retry_and_worker_preserve_binding(client, test_db_session, extractor):
    headers = await login(client)
    event, _, first = await start(client, test_db_session, headers)
    alias = payload()
    await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    clip = extractor["clips"][first.json()["clipId"]]
    clip.update(status="failed", error={"code": "EXPORT_FAILED", "message": "private-secret"})
    before = sum(r.method == "POST" for r in extractor["requests"])
    found = await client.get(f"{API}/{event}/exports")
    assert found.json()["status"] == "failed"
    assert sum(r.method == "POST" for r in extractor["requests"]) == before
    extractor["lost"] = True
    retry = await client.post(
        f"{API}/{event}/exports", json={**alias, "retryFailed": True}, headers=headers
    )
    assert retry.status_code == 504
    job = await test_db_session.scalar(select(MediaClipJob))
    await test_db_session.refresh(job)
    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await test_db_session.commit()
    await MediaJobWorker().run_once(test_db_session)
    response = await client.get(f"{API}/{event}/exports/{alias['requestId']}")
    assert response.json()["status"] == "processing"
    assert response.json()["clipId"] == first.json()["clipId"] and len(extractor["clips"]) == 1


async def test_alias_survives_legacy_delete(client, test_db_session, extractor):
    headers = await login(client, "admin")
    event, _, first = await start(client, test_db_session, headers)
    alias = payload()
    await client.post(f"{API}/{event}/exports", json=alias, headers=headers)
    deleted = await client.delete(f"/api/v1/media/clips/{first.json()['clipId']}", headers=headers)
    assert deleted.status_code == 204
    response = await client.get(f"{API}/{event}/exports/{alias['requestId']}")
    assert response.status_code == 200 and response.json()["status"] == "deleted"
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 2
