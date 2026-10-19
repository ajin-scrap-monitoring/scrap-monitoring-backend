"""Synthetic operational replay: durable cooldown, safe gating and event revision history."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from test_monitoring_v2 import DEVICE, NOW, SOURCE, assessment, login, send, status

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2AlertLink, EdgeV2Operation
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source

BASE = "/api/v2/monitoring"


@pytest.fixture(autouse=True)
def operations_config(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "true")
    monkeypatch.setenv("ALERT_COOLDOWN_SECONDS", "1800")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def tick(client, db, ratio, seconds, worker=None):
    when = NOW + timedelta(seconds=seconds)
    s = status(f"status-{seconds}", when=when, sequence=10000 + seconds)
    await send(client, s)
    await send(client, assessment(f"state-{seconds}", ratio=ratio, when=when))
    # Receipt time advances with the synthetic clock, independently of observation time.
    await db.execute(
        update(EdgeV2Message)
        .where(EdgeV2Message.message_id == s["message_id"])
        .values(received_at=when)
    )
    await db.commit()
    await (worker or EdgeV2OperationsWorker()).run_once(db, now=when)
    return await build_snapshot(db, monitoring_source("pit-demo"), when)


async def count(db, model):
    return await db.scalar(select(func.count()).select_from(model))


async def test_hysteresis_cooldown_and_new_worker_replay(client, test_db_session):
    db = test_db_session
    r = await tick(client, db, 0.91, 0)
    assert r.summary.collectionThresholdPercent == 90
    assert r.summary.recoveryThresholdPercent == 85
    assert r.operations.effectiveState == "CRITICAL"
    assert await count(db, AlertLog) == 1
    assert (await tick(client, db, 0.89, 1)).operations.effectiveState == "CRITICAL"
    assert (await tick(client, db, 0.85, 2)).operations.effectiveState == "WARNING"
    assert (await tick(client, db, 0.74, 3)).operations.effectiveState == "NORMAL"
    assert (await tick(client, db, 0.95, 4)).operations.effectiveState == "CRITICAL"
    assert await count(db, AlertLog) == 1
    # A recreated worker has no in-memory cooldown but must preserve the DB result.
    await EdgeV2OperationsWorker().run_once(db, now=NOW + timedelta(seconds=5))
    assert await count(db, AlertLog) == 1
    assert (await tick(client, db, 0.96, 1799)).operations.effectiveState == "CRITICAL"
    assert await count(db, AlertLog) == 1
    await tick(client, db, 0.96, 1800)
    assert await count(db, AlertLog) == 2
    assert await count(db, EdgeV2AlertLink) == 2
    for model in (ScrapMetric, CollectionLog, MediaClipJob):
        assert await count(db, model) == 0


async def test_custom_pit_thresholds_and_recovery(client, test_db_session):
    db = test_db_session
    db.add(
        PitConfig(
            id="pit-demo",
            name="Synthetic",
            warning_threshold_percent=70,
            critical_threshold_percent=95,
        )
    )
    await db.commit()
    assert (await tick(client, db, 0.94, 0)).operations.effectiveState == "WARNING"
    assert (await tick(client, db, 0.95, 1)).operations.effectiveState == "CRITICAL"
    assert (await tick(client, db, 0.91, 2)).operations.effectiveState == "CRITICAL"
    assert (await tick(client, db, 0.90, 3)).operations.effectiveState == "WARNING"


async def test_exact_thresholds_ignore_display_rounding(client, test_db_session):
    db = test_db_session
    assert (await tick(client, db, 0.899999999, 0)).operations.effectiveState == "WARNING"
    assert await count(db, AlertLog) == 0
    assert (await tick(client, db, 0.90, 1)).operations.effectiveState == "CRITICAL"
    assert (await tick(client, db, 0.850000001, 2)).operations.effectiveState == "CRITICAL"
    assert (await tick(client, db, 0.85, 3)).operations.effectiveState == "WARNING"


@pytest.mark.parametrize(
    "case", ["disabled", "unavailable", "stale", "no_status", "clock", "bad_thresholds"]
)
async def test_invalid_or_disabled_data_never_alerts(client, test_db_session, monkeypatch, case):
    db = test_db_session
    a = assessment(ratio=0.99)
    if case == "disabled":
        monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "false")
        get_settings.cache_clear()
    elif case == "unavailable":
        a.update(fill_estimate=None, fill_state="UNAVAILABLE", decision_state="UNAVAILABLE")
        a["capabilities"]["fill"] = "NOT_VALIDATED"
    elif case == "stale":
        a = assessment(ratio=0.99, when=NOW - timedelta(seconds=30))
    elif case == "clock":
        a = assessment(ratio=0.99, when=NOW + timedelta(minutes=1))
    elif case == "bad_thresholds":
        db.add(
            PitConfig(
                id="pit-demo",
                name="Synthetic invalid thresholds",
                warning_threshold_percent=89,
                critical_threshold_percent=90,
            )
        )
        await db.commit()
    if case != "no_status":
        await send(client, status())
    await send(client, a)
    await EdgeV2OperationsWorker().run_once(db, now=NOW)
    assert await count(db, AlertLog) == 0
    assert await count(db, EdgeV2Operation) == 0


async def test_status_late_arrival_can_enable_already_stored_state(client, test_db_session):
    await send(client, assessment(ratio=0.99))
    worker = EdgeV2OperationsWorker()
    await worker.run_once(test_db_session, now=NOW)
    assert await count(test_db_session, AlertLog) == 0
    await send(client, status())
    await worker.run_once(test_db_session, now=NOW)
    assert await count(test_db_session, AlertLog) == 1


async def test_invalid_data_preserves_hysteresis_and_cooldown(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    a = assessment("invalid", when=NOW + timedelta(seconds=1))
    a.update(fill_estimate=None, fill_state="UNAVAILABLE", decision_state="UNAVAILABLE")
    await send(client, a)
    await EdgeV2OperationsWorker().run_once(db, now=NOW + timedelta(seconds=1))
    result = await build_snapshot(db, monitoring_source("pit-demo"), NOW + timedelta(seconds=1))
    assert result.operations.lastKnownState == "CRITICAL"
    assert result.operations.effectiveState == "ERROR"
    assert result.summary.loadPercent is None
    assert (await tick(client, db, 0.89, 2)).operations.effectiveState == "CRITICAL"
    assert await count(db, AlertLog) == 1


async def test_past_state_event_replay_and_old_observation_do_not_reset(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    await send(client, assessment("older", 0.1, NOW - timedelta(seconds=1)))
    # An EVENT must not change current loading state, even when it looks like collection.
    event = collection("event-1", 3, "RESOLVED")
    await send(client, event)
    await EdgeV2OperationsWorker().run_once(db, now=NOW)
    assert (await db.get(EdgeV2Operation, "pit-demo")).state == "CRITICAL"
    delayed = assessment("delayed", 0.1, NOW + timedelta(seconds=1))
    delayed["observed_from"] = delayed["observed_to"] = (NOW - timedelta(seconds=1)).isoformat()
    await send(client, delayed)
    await EdgeV2OperationsWorker().run_once(db, now=NOW + timedelta(seconds=1))
    row = await db.get(EdgeV2Operation, "pit-demo")
    assert row.message_id == "state-0"
    assert row.state == "CRITICAL"
    assert await count(db, AlertLog) == 1
    assert await count(db, CollectionLog) == 0


async def test_commit_failure_rolls_back_cursor_and_alert_then_recovers(
    client, test_db_session, monkeypatch
):
    db = test_db_session
    await send(client, status())
    await send(client, assessment(ratio=0.99))
    original = db.commit

    async def fail_commit():
        raise OperationalError("synthetic commit", {}, Exception("synthetic"))

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(OperationalError):
        await EdgeV2OperationsWorker().run_once(db, now=NOW)
    assert await count(db, AlertLog) == 0
    assert await count(db, EdgeV2Operation) == 0
    monkeypatch.setattr(db, "commit", original)
    await EdgeV2OperationsWorker().run_once(db, now=NOW)
    assert await count(db, AlertLog) == 1
    assert await count(db, EdgeV2AlertLink) == 1


async def test_alert_history_notification_and_pending_snapshot(client, test_db_session):
    db = test_db_session
    initial = await tick(client, db, 0.99, 0)
    _, session = await login(client)
    r = await client.get(BASE + "/alerts", params={"pit_id": "pit-demo"})
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["loadPercent"] == 99
    assert item["assessmentId"] == "state-0"
    notifications = (await client.get("/api/v1/notifications")).json()
    assert notifications["items"][0]["id"] == item["notificationId"]
    assert notifications["items"][0]["severity"] == "error"
    r = await client.patch(
        "/api/v1/notifications/" + item["notificationId"],
        json={"read": True},
        headers={"X-CSRF-Token": session.csrfToken},
    )
    assert r.status_code == 200
    r = await client.get(BASE + "/alerts", params={"pit_id": "pit-demo"})
    assert r.json()["items"][0]["readAt"] is not None
    await send(client, assessment("pending", 0.2, NOW + timedelta(seconds=1)))
    pending = await build_snapshot(db, monitoring_source("pit-demo"), NOW + timedelta(seconds=1))
    assert pending.operations.lastKnownState == "CRITICAL"
    assert pending.operations.effectiveState == "CRITICAL"
    assert pending.operations.evaluationSource == "RECENT_PERSISTED"
    assert pending.operations.lastEvaluatedAt == NOW
    assert pending.snapshotId != initial.snapshotId


def collection(identifier, revision, stage, kind="COLLECTION"):
    a = assessment(f"{identifier}-rev-{revision}")
    a.update(
        assessment_type="EVENT",
        assessment_id=identifier,
        revision=revision,
        event={"kind": kind, "stage": stage},
    )
    return a


async def test_collection_latest_revision_and_complete_audit_history(client, test_db_session):
    # A higher revision can arrive before lower revisions. Retraction stays visible.
    await send(client, collection("collection-1", 4, "RETRACTED"))
    await send(client, collection("collection-1", 2, "CONFIRMED"))
    await send(client, collection("collection-1", 1, "SUSPECTED"))
    await send(client, collection("collection-1", 3, "RESOLVED"))
    replay = collection("collection-1", 4, "RETRACTED")
    replay["message_id"] = "new-envelope-same-revision"
    await send(client, replay)
    await send(client, collection("grapple-1", 1, "CONFIRMED", "GRAPPLE_PRESENT"))
    await login(client)
    r = await client.get(BASE + "/collection-events", params={"pit_id": "pit-demo"})
    assert r.status_code == 200
    assert r.json()["totalItems"] == 1
    item = r.json()["items"][0]
    assert item["revision"] == 4 and item["event"]["stage"] == "RETRACTED"
    assert item["message_id"] == "collection-1-rev-4"
    r = await client.get(
        BASE + "/collection-events/collection-1/revisions", params={"pit_id": "pit-demo"}
    )
    assert r.json()["totalItems"] == 4
    assert [i["revision"] for i in r.json()["items"]] == [4, 3, 2, 1]
    assert await count(test_db_session, CollectionLog) == 0
    assert await count(test_db_session, AlertLog) == 0


async def test_collection_pagination_and_latest_kind_change(client):
    for i in range(3):
        await send(client, collection(f"collection-{i}", 1, "CONFIRMED"))
    # The newest judgment is not COLLECTION; the older COLLECTION must not resurface.
    await send(client, collection("collection-0", 2, "RETRACTED", "GRAPPLE_PRESENT"))
    await login(client)
    r = await client.get(
        BASE + "/collection-events", params={"pit_id": "pit-demo", "limit": 1, "offset": 1}
    )
    assert r.status_code == 200
    assert r.json()["totalItems"] == 2
    assert len(r.json()["items"]) == 1
    for params in ({"limit": 101}, {"offset": -1}):
        r = await client.get(BASE + "/collection-events", params={"pit_id": "pit-demo", **params})
        assert r.status_code == 422


@pytest.mark.parametrize(
    "path", ["/alerts", "/collection-events", "/collection-events/collection-1/revisions"]
)
async def test_history_requires_session_and_explicit_source(client, path):
    r = await client.get(BASE + path, params={"pit_id": "pit-demo"})
    assert r.status_code == 401
    await login(client)
    r = await client.get(BASE + path, params={"pit_id": "unmapped-pit"})
    assert r.status_code == 404


async def test_history_isolates_devices_and_assessment_ids(client, monkeypatch):
    other = {**DEVICE, "edge_id": "edge-other", "token": "synthetic-other-token"}
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE, other]))
    get_settings.cache_clear()
    payload = collection("same-id", 10, "CONFIRMED")
    payload["edge_id"] = "edge-other"
    r = await client.post(
        "/api/edge/v2/assessments",
        json=payload,
        headers={
            "Authorization": "Bearer synthetic-other-token",
            "Idempotency-Key": payload["message_id"],
        },
    )
    assert r.status_code == 201
    await send(client, collection("same-id", 1, "RETRACTED"))
    await login(client)
    r = await client.get(BASE + "/collection-events", params={"pit_id": "pit-demo"})
    assert r.json()["totalItems"] == 1
    assert r.json()["items"][0]["revision"] == 1


async def test_worker_shutdown_cancels_owned_task(monkeypatch):
    worker = EdgeV2OperationsWorker()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def wait_forever():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(worker, "_run", wait_forever)
    worker.start()
    await entered.wait()
    worker.start()
    await worker.stop()
    assert stopped.is_set()
    assert worker._task is None
