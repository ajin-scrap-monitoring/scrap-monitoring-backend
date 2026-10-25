"""Fresh event recording, explicit categories, and late-request protection."""

import importlib
import json
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from test_edge_v2_operations import operations_config, tick  # noqa: F401
from test_monitoring_v2 import NOW

from src.app.core.config import Settings, get_settings
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.services.media_client import MediaServiceError, media_client
from src.app.services.media_job_service import MediaJobWorker, job_snapshot, process_job
from src.app.services.v2_event_clip_service import CURSOR_KIND, queue_recent_events


@pytest.fixture
async def camera(monkeypatch):
    clock = {"now": NOW}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.media_job_service.datetime", Clock)
    monkeypatch.setattr(
        importlib.import_module("src.app.services.recording_service"), "datetime", Clock
    )
    monkeypatch.setattr("src.app.services.collection_ended_service.datetime", Clock)
    monkeypatch.setattr("src.app.services.error_history_service.datetime", Clock)
    settings = get_settings()
    for key, value in {
        "MEDIA_SERVICE_URL": "http://media.test/api/v1",
        "MEDIA_API_TOKEN": SecretStr("synthetic-media-token"),
        "MEDIA_CAMERA_ID": "camera-main",
        "MEDIA_PIT_ID": "pit-demo",
        "MEDIA_V2_EVENT_CLIPS_ENABLED": True,
        "MEDIA_V2_EVENT_CLIP_MAX_DELAY_SECONDS": 30,
    }.items():
        monkeypatch.setattr(settings, key, value)
    remote = {"clock": clock, "clips": {}, "requests": [], "lose": False, "fail": False}

    async def handle(request):
        remote["requests"].append(request)
        assert request.headers["authorization"] == "Bearer synthetic-media-token"
        if remote["fail"]:
            return httpx.Response(503, json={"error": {"code": "STORAGE_UNAVAILABLE"}})
        if request.method == "POST":
            data = json.loads(request.content)
            assert set(data) == {"event_id", "camera_id"}
            event_id = data["event_id"]
            when = clock["now"]
            cid = str(uuid5(NAMESPACE_URL, event_id))
            remote["clips"].setdefault(
                event_id,
                {
                    "clip_id": cid,
                    "event_id": event_id,
                    "camera_id": "camera-main",
                    "status": "ready",
                    "accepted_at": when.isoformat(),
                    "requested_start_at": (when - timedelta(seconds=30)).isoformat(),
                    "requested_end_at": (when + timedelta(seconds=30)).isoformat(),
                    "coverage": "complete",
                    "recording_policy": {
                        "pre_roll_seconds": 30,
                        "post_roll_seconds": 30,
                        "retention_days": 30,
                    },
                    "actual_start_at": (when - timedelta(seconds=20)).isoformat(),
                    "actual_end_at": (when + timedelta(seconds=30)).isoformat(),
                    "gaps": [],
                    "completed_at": when.isoformat(),
                    "expires_at": (when + timedelta(days=30)).isoformat(),
                    "deleted_at": None,
                    "content_path": f"/api/v1/clips/{cid}/content",
                    "error": None,
                },
            )
            if remote["lose"]:
                remote["lose"] = False
                raise httpx.ReadTimeout("Synthetic lost receipt", request=request)
            fields = (
                "clip_id",
                "event_id",
                "camera_id",
                "status",
                "accepted_at",
                "requested_start_at",
                "requested_end_at",
            )
            return httpx.Response(202, json={key: remote["clips"][event_id][key] for key in fields})
        if request.url.path.endswith("/clips"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        v
                        for v in remote["clips"].values()
                        if v["event_id"] == request.url.params.get("event_id")
                    ],
                    "next_cursor": None,
                },
            )
        item = next(v for v in remote["clips"].values() if request.url.path.endswith(v["clip_id"]))
        return httpx.Response(200, json=item)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(media_client, "_client", client)
        yield remote


async def transition(
    db, kind="DEVICE_STATE_CHANGED", *, when=NOW, payload=None, site="site-demo", edge="edge-demo"
):
    data = {
        "pitId": "pit-demo",
        "previous": "HEALTHY",
        "current": "MEASUREMENT_UNAVAILABLE",
        "simulated": True,
    }
    data.update(payload or {})
    row = EdgeV2Transition(site_id=site, edge_id=edge, occurred_at=when, kind=kind, payload=data)
    db.add(row)
    await db.commit()
    return row


async def jobs(db):
    return list((await db.scalars(select(MediaClipJob).order_by(MediaClipJob.id))).all())


def posts(camera):
    return [r for r in camera["requests"] if r.method == "POST"]


async def pending(db, camera):
    await queue_recent_events(db, "session", NOW)
    event = await transition(db)
    await queue_recent_events(db, "session", NOW)
    row = (await jobs(db))[0]
    assert row.event_id == f"evt-v2-transition-{event.id}" and job_snapshot(row) is None
    assert camera["requests"] == []
    return row


def test_automatic_clips_default_disabled():
    assert Settings().MEDIA_V2_EVENT_CLIPS_ENABLED is False


@pytest.mark.parametrize("delay", [0, 301])
def test_automatic_clip_delay_is_bounded(delay):
    with pytest.raises(ValidationError):
        Settings(MEDIA_V2_EVENT_CLIP_MAX_DELAY_SECONDS=delay)


async def test_initialization_restart_and_reenable_never_backfill(
    test_db_session, camera, monkeypatch
):
    db = test_db_session
    await transition(db)
    worker = MediaJobWorker()
    await worker.run_once(db)
    assert not await jobs(db)
    await transition(db)
    await MediaJobWorker().run_once(db)
    assert not await jobs(db)
    monkeypatch.setattr(get_settings(), "MEDIA_V2_EVENT_CLIPS_ENABLED", False)
    await worker.run_once(db)
    await transition(db)
    monkeypatch.setattr(get_settings(), "MEDIA_V2_EVENT_CLIPS_ENABLED", True)
    await worker.run_once(db)
    assert not await jobs(db) and not posts(camera)


async def test_real_v2_alarm_is_queued_once_and_reads_have_no_side_effects(
    client, test_db_session, camera
):
    db = test_db_session
    worker = MediaJobWorker()
    await worker.run_once(db)
    await tick(client, db, 0.91, 0)
    assert not await jobs(db)  # Neither sensor receipt nor the operations worker calls media.
    await worker.run_once(db)
    row = (await jobs(db))[0]
    assert row.event_id == "evt-alt-1" and row.category == "v2_alert"
    assert len(posts(camera)) == 1
    await worker.run_once(db)
    await tick(client, db, 0.95, 1)
    await worker.run_once(db)
    assert len(await jobs(db)) == len(posts(camera)) == 1
    await client.post("/api/v1/session", json={"username": "operator", "password": "operator1234"})
    for _ in range(2):
        data = (await client.get("/api/v1/events?pit_id=pit-demo&category=alert")).json()
        assert data["items"][0]["recordingId"] == row.clip_id
    assert len(posts(camera)) == 1


async def test_collection_error_categories_and_history_identity(client, test_db_session, camera):
    db = test_db_session
    worker = MediaJobWorker()
    await worker.run_once(db)
    metadata = {
        "detectionId": "synthetic-cycle",
        "scopeToken": "synthetic-scope",
        "confirmedCompletion": False,
    }
    started = await transition(
        db,
        "COLLECTION_DETECTED",
        payload=metadata,
    )
    error = await transition(db)
    await transition(db, payload={"previous": "MEASUREMENT_UNAVAILABLE", "current": "HEALTHY"})
    await transition(
        db,
        "MEASUREMENT_VALIDITY_CHANGED",
        payload={"previousValid": True, "currentValid": False, "reasonCodes": ["MEDIAN_WARMING"]},
    )
    await transition(db, "COLLECTION_DETECTED", payload={"detectionId": "start-only"})
    await worker.run_once(db)
    assert {j.event_id for j in await jobs(db)} == {
        f"evt-auto-col-start-{started.id}",
        f"evt-v2-transition-{error.id}",
    }
    camera["clock"]["now"] += timedelta(seconds=3)
    await worker.run_once(db)
    await client.post("/api/v1/session", json={"username": "operator", "password": "operator1234"})
    opening = (await client.get("/api/v1/recordings?pit_id=pit-demo&category=collection")).json()[
        "items"
    ][0]
    assert opening["eventEndedAt"] is None and opening["eventWindowCovered"] is None
    camera["clock"]["now"] = NOW + timedelta(seconds=10)
    ended = await transition(
        db, "COLLECTION_SETTLED", when=camera["clock"]["now"], payload=metadata
    )
    await worker.run_once(db)
    for category in ("collection", "error"):
        response = await client.get(
            "/api/v1/recordings", params={"pit_id": "pit-demo", "category": category}
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["eventCategory"] == category
        assert items[0]["eventSimulated"] is True and items[0]["eventOccurredAt"] is not None
        assert items[0]["status"] == "available"
        history = (
            await client.get(
                "/api/v1/events",
                params={"pit_id": "pit-demo", "category": category, "collection_scope": "all"},
            )
        ).json()
        identifier = f"evt-auto-col-{ended.id}" if category == "collection" else items[0]["detail"]
        matched = next(e for e in history["items"] if e["id"] == identifier)
        assert matched["recordingId"] == items[0]["id"]
        if category == "collection":
            assert datetime.fromisoformat(items[0]["eventEndedAt"]) == camera["clock"]["now"]
            assert items[0]["eventWindowCovered"] is True
    assert len(posts(camera)) == 2


async def test_collection_end_must_match_and_short_clip_is_not_the_whole_collection(
    client, test_db_session, camera
):
    db = test_db_session
    worker = MediaJobWorker()
    await worker.run_once(db)
    data = {"detectionId": "long-cycle", "scopeToken": "long-scope", "confirmedCompletion": False}
    await transition(db, "COLLECTION_DETECTED", payload=data)
    await worker.run_once(db)
    camera["clock"]["now"] += timedelta(seconds=3)
    await worker.run_once(db)
    await client.post("/api/v1/session", json={"username": "operator", "password": "operator1234"})
    camera["clock"]["now"] = NOW + timedelta(seconds=120)
    for payload, site in [
        ({**data, "scopeToken": "other-scope"}, "site-demo"),
        (data, "other-site"),
        ({**data, "pitId": "other-pit"}, "site-demo"),
    ]:
        await transition(
            db, "COLLECTION_SETTLED", when=camera["clock"]["now"], payload=payload, site=site
        )

    async def item():
        return (await client.get("/api/v1/recordings?pit_id=pit-demo&category=collection")).json()[
            "items"
        ][0]

    assert (await item())["eventEndedAt"] is None
    await transition(db, "COLLECTION_SETTLED", when=camera["clock"]["now"], payload=data)
    result = await item()
    assert result["eventEndedAt"] is not None and result["eventWindowCovered"] is False
    assert len(posts(camera)) == 1  # An end is metadata, not an invented extend/stop API.


async def test_measurement_unavailable_is_an_error_but_recovery_is_not(test_db_session, camera):
    db = test_db_session
    await queue_recent_events(db, "session", NOW)
    error = await transition(
        db,
        "MEASUREMENT_VALIDITY_CHANGED",
        payload={"previousValid": True, "currentValid": False, "reasonCodes": ["NO_VALID_POINTS"]},
    )
    await transition(
        db, "MEASUREMENT_VALIDITY_CHANGED", payload={"previousValid": False, "currentValid": True}
    )
    await queue_recent_events(db, "session", NOW)
    assert [j.event_id for j in await jobs(db)] == [f"evt-v2-transition-{error.id}"]


async def test_stale_future_wrong_pit_and_unlinked_alerts_are_skipped(test_db_session, camera):
    db = test_db_session
    await queue_recent_events(db, "session", NOW)
    await transition(db, when=NOW - timedelta(seconds=31))
    await transition(db, when=NOW + timedelta(seconds=1))
    await transition(db, payload={"pitId": "other-pit"})
    await transition(db, site="other-site")
    await transition(db, "ALERT_CREATED", payload={"alertId": "bad"})
    alert = AlertLog(
        pit_id="pit-demo",
        triggered_at=NOW,
        fill_ratio_percent=95,
        channel="LOG",
        status="RECORDED",
        message="Synthetic",
    )
    db.add(alert)
    await db.flush()
    db.add(
        EdgeV2AlertLink(
            alert_id=alert.id,
            pit_id="pit-demo",
            site_id="other-site",
            edge_id="edge-demo",
            assessment_id="synthetic",
            message_id="synthetic",
            observed_at=NOW,
        )
    )
    await transition(db, "ALERT_CREATED", payload={"alertId": alert.id})
    await queue_recent_events(db, "session", NOW)
    assert not await jobs(db) and not posts(camera)


async def test_rollback_keeps_cursor_and_job_atomic(test_db_session, camera, monkeypatch):
    db = test_db_session
    await queue_recent_events(db, "session", NOW)
    event = await transition(db)
    commit = db.commit

    async def failed():
        raise RuntimeError("Synthetic failed commit")

    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", failed)
        with pytest.raises(RuntimeError):
            await queue_recent_events(db, "session", NOW)
    await db.rollback()
    assert not await jobs(db)
    await queue_recent_events(db, "session", NOW)
    await queue_recent_events(db, "session", NOW)
    assert len(await jobs(db)) == 1
    assert (await jobs(db))[0].event_id == f"evt-v2-transition-{event.id}"
    assert camera["requests"] == []
    assert db.commit == commit


async def test_cursor_work_is_bounded(test_db_session, camera):
    db = test_db_session
    await queue_recent_events(db, "session", NOW)
    for _ in range(41):
        await transition(db)
    for expected in (20, 40, 41):
        await queue_recent_events(db, "session", NOW)
        assert len(await jobs(db)) == expected
    cursor = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", CURSOR_KIND))
    assert set(cursor.payload) == {"sessionToken", "afterId"}
    assert camera["requests"] == []


async def test_late_commit_with_smaller_id_is_not_lost(test_db_session, test_engine, camera):
    from sqlalchemy.ext.asyncio import AsyncSession

    if test_engine.dialect.name != "postgresql":
        pytest.skip("Out-of-order commits require isolated PostgreSQL connections")
    db = test_db_session
    await queue_recent_events(db, "session", NOW)
    async with AsyncSession(test_engine, expire_on_commit=False) as first:
        lower = EdgeV2Transition(
            site_id="site-demo",
            edge_id="edge-demo",
            occurred_at=NOW,
            kind="DEVICE_STATE_CHANGED",
            payload={"pitId": "pit-demo", "previous": "HEALTHY", "current": "CONFIG_ERROR"},
        )
        first.add(lower)
        await first.flush()
        async with AsyncSession(test_engine, expire_on_commit=False) as second:
            higher = await transition(second)
        assert lower.id < higher.id
        await queue_recent_events(db, "session", NOW)
        assert len(await jobs(db)) == 1
        await first.commit()
    await queue_recent_events(db, "session", NOW)
    assert {j.event_id for j in await jobs(db)} == {
        f"evt-v2-transition-{lower.id}",
        f"evt-v2-transition-{higher.id}",
    }
    assert camera["requests"] == []


async def test_expired_unsent_job_does_not_create_present_day_footage(test_db_session, camera):
    row = await pending(test_db_session, camera)
    camera["clock"]["now"] += timedelta(seconds=31)
    with pytest.raises(MediaServiceError, match="EVENT_CLIP_WINDOW_EXPIRED"):
        await process_job(row, test_db_session)
    assert row.status == "blocked" and row.next_attempt_at is None
    assert not posts(camera)


async def test_lost_create_receipt_is_recovered_by_get_after_deadline(test_db_session, camera):
    row = await pending(test_db_session, camera)
    camera["lose"] = True
    with pytest.raises(MediaServiceError):
        await process_job(row, test_db_session)
    camera["clock"]["now"] += timedelta(seconds=31)
    assert await process_job(row, test_db_session) == 200
    assert len(posts(camera)) == 1 and row.clip_id is not None
    assert row.status == "ready" and job_snapshot(row).status == "ready"
    assert row.snapshot["_v2Simulated"] is True


async def test_unaccepted_outage_is_not_retried_as_a_late_post(test_db_session, camera):
    row = await pending(test_db_session, camera)
    camera["fail"] = True
    with pytest.raises(MediaServiceError):
        await process_job(row, test_db_session)
    camera["fail"] = False
    camera["clock"]["now"] += timedelta(seconds=31)
    with pytest.raises(MediaServiceError, match="EVENT_CLIP_WINDOW_EXPIRED"):
        await process_job(row, test_db_session)
    assert len(posts(camera)) == 1 and row.clip_id is None


async def test_source_change_blocks_cross_service_recovery(test_db_session, camera, monkeypatch):
    row = await pending(test_db_session, camera)
    camera["lose"] = True
    with pytest.raises(MediaServiceError):
        await process_job(row, test_db_session)
    before = len(camera["requests"])
    monkeypatch.setattr(get_settings(), "MEDIA_CAMERA_ID", "other-camera")
    monkeypatch.setattr(get_settings(), "MEDIA_V2_EVENT_CLIPS_ENABLED", False)
    with pytest.raises(MediaServiceError, match="MEDIA_CLIP_SOURCE_CHANGED"):
        await process_job(row, test_db_session)
    assert len(camera["requests"]) == before


async def test_disable_blocks_new_creation_but_accepted_clips_remain_pollable(
    test_db_session, camera, monkeypatch
):
    db = test_db_session
    row = await pending(db, camera)
    await process_job(row, db)
    camera["clock"]["now"] += timedelta(hours=1)
    monkeypatch.setattr(get_settings(), "MEDIA_V2_EVENT_CLIPS_ENABLED", False)
    await process_job(row, db)
    assert row.status == "ready" and len(posts(camera)) == 1
    monkeypatch.setattr(get_settings(), "MEDIA_V2_EVENT_CLIPS_ENABLED", True)
    camera["clock"]["now"] = NOW
    await transition(db)
    await queue_recent_events(db, "session", NOW)
    other = (await jobs(db))[-1]
    monkeypatch.setattr(get_settings(), "MEDIA_V2_EVENT_CLIPS_ENABLED", False)
    with pytest.raises(MediaServiceError, match="V2_EVENT_CLIPS_DISABLED"):
        await process_job(other, db)
    assert len(posts(camera)) == 1
