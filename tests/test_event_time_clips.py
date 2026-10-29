"""Explicit event-time extraction, immutable retries and legacy wire compatibility."""

import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_media_control import (  # noqa: F401
    CLIP_ID,
    NOW,
    FakeMedia,
    due,
    fake_media,
    login,
    seed_alert,
    utc,
)

from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.schemas.media_service import ClipRequest, event_time_text
from src.app.services.media_client import media_client
from src.app.services.media_job_service import MediaJobWorker, job_snapshot, process_job

__all__ = ["fake_media"]

API = "/api/v1/media/clips"


class HistoricalMedia(FakeMedia):
    def __init__(self):
        super().__init__()
        self.event_at = None
        self.wrong = None

    def receipt(self):
        result = super().receipt()
        if self.event_at is not None:
            when = datetime.fromisoformat(self.event_at.replace("Z", "+00:00"))
            result.update(
                event_at=self.event_at,
                requested_start_at=utc(when - timedelta(seconds=30)),
                requested_end_at=utc(when + timedelta(seconds=30)),
            )
            if self.wrong == "omit":
                result.pop("event_at")
            elif self.wrong == "shift":
                result["event_at"] = utc(when + timedelta(seconds=1))
        return result

    def detail(self):
        result = super().detail()
        if self.event_at is not None and self.status in ("ready", "deleted", "expired"):
            when = datetime.fromisoformat(self.event_at.replace("Z", "+00:00"))
            result.update(
                actual_start_at=utc(when - timedelta(seconds=30)),
                actual_end_at=utc(when + timedelta(seconds=30)),
            )
        return result

    async def handle(self, request):
        if request.method != "POST" or self.error_status:
            return await super().handle(request)
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-media-token"
        assert "cookie" not in request.headers
        payload = json.loads(request.content)
        assert set(payload) == {"event_id", "camera_id", "event_at"}
        assert payload["event_at"].endswith("Z") and len(payload["event_at"].split(".")[1]) == 4
        if self.event_at is not None:
            assert payload["event_at"] == self.event_at
        self.event_at = payload["event_at"]
        self.event_id = payload["event_id"]
        created = not self.created
        self.created = True
        self.status = "processing"
        if self.lose_first_response and created:
            raise httpx.ReadTimeout("Response lost", request=request)
        return httpx.Response(202 if created else 200, json=self.receipt())


@pytest.fixture
async def historical_media(fake_media, monkeypatch):
    fake = HistoricalMedia()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        monkeypatch.setattr(media_client, "_client", client)
        yield fake


async def old_alert(db):
    identifier = await seed_alert(db)
    row = await db.scalar(select(AlertLog))
    row.triggered_at = NOW - timedelta(days=1) + timedelta(microseconds=123456)
    await db.commit()
    return identifier, row.triggered_at


async def test_explicit_request_uses_persisted_event_time(
    client, test_db_session, historical_media
):
    identifier, when = await old_alert(test_db_session)
    headers = await login(client)
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=headers
    )
    assert response.status_code == 202, response.text
    assert historical_media.event_at == event_time_text(when)
    assert response.json()["event_at"] == event_time_text(when)
    assert response.json()["accepted_at"] != response.json()["event_at"]
    assert "use_event_time" not in json.loads(historical_media.requests[0].content)
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.snapshot["_eventTimeRequest"]["version"] == 1
    assert job_snapshot(job).event_at is not None
    jobs = await client.get("/api/v1/media/clip-jobs")
    assert jobs.status_code == 200 and jobs.json()[0]["time_basis"] == "EVENT_OCCURRED_AT"
    assert "_eventTimeRequest" not in jobs.text


async def test_legacy_request_wire_and_response_are_unchanged(client, test_db_session, fake_media):
    identifier = await seed_alert(test_db_session)
    response = await client.post(API, json={"event_id": identifier}, headers=await login(client))
    assert response.status_code == 202, response.text
    assert "event_at" not in json.loads(fake_media.requests[0].content)
    assert "event_at" not in response.json()
    assert (await client.get("/api/v1/media/clip-jobs")).json()[0]["time_basis"] == "ACCEPTED_AT"


async def test_history_time_requires_admin_csrf_and_existing_event(
    client, test_db_session, historical_media
):
    identifier, _ = await old_alert(test_db_session)
    body = {"event_id": identifier, "use_event_time": True}
    assert (await client.post(API, json=body)).status_code == 401
    viewer = await login(client, "operator")
    assert (await client.post(API, json=body, headers=viewer)).status_code == 403
    admin = await login(client)
    assert (await client.post(API, json=body)).status_code == 403
    assert (
        await client.post(API, json={**body, "event_id": "evt-alt-9999"}, headers=admin)
    ).status_code == 404
    assert (
        await client.post(API, json={**body, "event_id": "evt-auto-col-start-1"}, headers=admin)
    ).status_code == 404
    assert historical_media.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        {"event_at": "2020-01-01T00:00:00.000Z"},
        {"use_event_time": "true"},
        {"use_event_time": 1},
        {"start_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_arbitrary_times_and_ambiguous_flags_are_rejected(client, historical_media, extra):
    response = await client.post(
        API, json={"event_id": "evt-alt-1", **extra}, headers=await login(client)
    )
    assert response.status_code == 422
    assert historical_media.requests == []


async def test_lost_receipt_restart_reuses_same_event_timestamp(
    client, test_engine, test_db_session, historical_media
):
    identifier, when = await old_alert(test_db_session)
    historical_media.lose_first_response = True
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
    )
    assert response.status_code == 504
    jobs = await client.get("/api/v1/media/clip-jobs")
    assert jobs.status_code == 200 and jobs.json()[0]["clip"] is None
    assert jobs.json()[0]["time_basis"] == "EVENT_OCCURRED_AT"
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        job = await db.scalar(select(MediaClipJob))
        assert job_snapshot(job) is None and job.snapshot["_eventTimeRequest"]["version"] == 1
        assert await process_job(job, db) == 200
        assert job.clip_id == CLIP_ID and job.snapshot["_eventTimeRequest"]["version"] == 1
    assert [json.loads(r.content)["event_at"] for r in historical_media.requests] == [
        event_time_text(when)
    ] * 2


async def test_background_retry_preserves_time_basis(client, test_db_session, historical_media):
    identifier, when = await old_alert(test_db_session)
    historical_media.lose_first_response = True
    r = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
    )
    assert r.status_code == 504
    await due(test_db_session)
    await MediaJobWorker().run_once(test_db_session)
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.clip_id == CLIP_ID and job.snapshot["_eventTimeRequest"]["version"] == 1
    assert historical_media.event_at == event_time_text(when)


async def test_existing_historical_mode_cannot_be_changed_to_live(
    client, test_db_session, historical_media
):
    identifier, _ = await old_alert(test_db_session)
    headers = await login(client)
    assert (
        await client.post(
            API, json={"event_id": identifier, "use_event_time": True}, headers=headers
        )
    ).status_code == 202
    response = await client.post(API, json={"event_id": identifier}, headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "CLIP_TIME_BASIS_CONFLICT"
    assert len(historical_media.requests) == 1


async def test_existing_live_job_cannot_be_retargeted_to_history(
    client, test_db_session, fake_media
):
    identifier = await seed_alert(test_db_session)
    headers = await login(client)
    assert (
        await client.post(API, json={"event_id": identifier}, headers=headers)
    ).status_code == 202
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=headers
    )
    assert response.status_code == 409 and len(fake_media.requests) == 1


@pytest.mark.parametrize("wrong", ["omit", "shift"])
async def test_wrong_remote_time_is_not_attached_to_event(
    client, test_db_session, historical_media, wrong
):
    identifier, _ = await old_alert(test_db_session)
    historical_media.wrong = wrong
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
    )
    assert (
        response.status_code == 502
        and response.json()["error"]["code"] == "MEDIA_TIME_BASIS_MISMATCH"
    )
    job = await test_db_session.scalar(select(MediaClipJob))
    assert (
        job.clip_id is None
        and job.status == "blocked"
        and job.snapshot["_eventTimeRequest"]["version"] == 1
    )


@pytest.mark.parametrize(
    "code,status,retry",
    [
        ("RECORDING_NOT_FOUND_AT_TIME", 404, False),
        ("RECORDING_NOT_READY", 409, True),
        ("RECORDING_NOT_ENABLED", 503, True),
        ("EVENT_TIME_IN_FUTURE", 400, False),
    ],
)
async def test_historical_errors_are_safe_and_do_not_fall_back_to_live(
    client, test_db_session, historical_media, code, status, retry
):
    identifier, _ = await old_alert(test_db_session)
    historical_media.error_status = status
    historical_media.error_code = code
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code and "synthetic-media-token" not in response.text
    job = await test_db_session.scalar(select(MediaClipJob))
    assert bool(job.next_attempt_at) == retry and job.snapshot["_eventTimeRequest"]["version"] == 1
    assert len(historical_media.requests) == 1


def test_wire_time_is_utc_millisecond_and_legacy_omits_null():
    request = ClipRequest(event_id="evt-alt-1", event_at="2026-01-01T09:00:00.123456+09:00")
    assert request.model_dump(mode="json")["event_at"] == "2026-01-01T00:00:00.123Z"
    assert "event_at" not in ClipRequest(event_id="evt-alt-1").model_dump(mode="json")


async def test_pending_history_request_is_blocked_after_media_source_change(
    client, test_db_session, historical_media, monkeypatch
):
    from src.app.core.config import get_settings
    from src.app.services.media_client import MediaServiceError

    identifier, _ = await old_alert(test_db_session)
    historical_media.lose_first_response = True
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
    )
    assert response.status_code == 504
    job = await test_db_session.scalar(select(MediaClipJob))
    monkeypatch.setattr(get_settings(), "MEDIA_SERVICE_URL", "http://different.test/api/v1")
    with pytest.raises(MediaServiceError) as exc:
        await process_job(job, test_db_session)
    assert exc.value.code == "MEDIA_CLIP_SOURCE_CHANGED"
    assert len(historical_media.requests) == 1 and job.clip_id is None


async def test_completed_history_poll_keeps_time_and_private_context(
    client, test_db_session, historical_media
):
    identifier, when = await old_alert(test_db_session)
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
    )
    assert response.status_code == 202
    historical_media.status = "ready"
    detail = await client.get(API + "/" + CLIP_ID)
    assert detail.status_code == 200, detail.text
    assert detail.json()["event_at"] == event_time_text(when)
    assert detail.json()["status"] == "ready"
    assert "_eventTimeRequest" not in detail.text
    job = await test_db_session.scalar(select(MediaClipJob))
    assert job.snapshot["_eventTimeRequest"]["eventAt"] == event_time_text(when)


async def test_existing_job_from_other_pit_is_not_dispatched(
    client, test_db_session, historical_media
):
    identifier, _ = await old_alert(test_db_session)
    headers = await login(client)
    assert (
        await client.post(
            API, json={"event_id": identifier, "use_event_time": True}, headers=headers
        )
    ).status_code == 202
    job = await test_db_session.scalar(select(MediaClipJob))
    job.pit_id = "different-pit"
    await test_db_session.commit()
    response = await client.post(
        API, json={"event_id": identifier, "use_event_time": True}, headers=headers
    )
    assert response.status_code == 404 and len(historical_media.requests) == 1


async def test_corrupt_pending_time_context_never_falls_back_to_live(
    client, test_db_session, historical_media
):
    from src.app.services.media_client import MediaServiceError

    identifier, _ = await old_alert(test_db_session)
    historical_media.lose_first_response = True
    assert (
        await client.post(
            API, json={"event_id": identifier, "use_event_time": True}, headers=await login(client)
        )
    ).status_code == 504
    job = await test_db_session.scalar(select(MediaClipJob))
    job.snapshot = {
        "_eventTimeRequest": {**job.snapshot["_eventTimeRequest"], "eventAt": "invalid"}
    }
    await test_db_session.commit()
    with pytest.raises(MediaServiceError) as exc:
        await process_job(job, test_db_session)
    assert exc.value.code == "MEDIA_TIME_CONTEXT_INVALID" and len(historical_media.requests) == 1
