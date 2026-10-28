"""History links identify shared event metadata, never promise existing footage."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import func, select
from test_daily_recordings import login
from test_event_recordings import API, BASE, NOW, cycle, day
from test_event_recordings import archive as archive

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric

HISTORY = "/api/v1/events"


async def history(client, **params):
    response = await client.get(
        HISTORY, params={"pit_id": "pit-demo", "collection_scope": "all", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def transition(kind, at=BASE, **overrides):
    payload = {
        "pitId": "pit-demo",
        "detectionId": "synthetic-cycle",
        "scopeToken": "synthetic-scope",
        **overrides.pop("payload", {}),
    }
    return EdgeV2Transition(
        kind=kind,
        occurred_at=at,
        payload=payload,
        **{"site_id": "site-demo", "edge_id": "edge-demo", **overrides},
    )


async def test_automatic_end_links_to_start_outside_history_range_without_new_jobs(
    client, test_db_session, archive
):
    db = test_db_session
    target = await cycle(db, end=BASE + timedelta(seconds=120))
    archive["days"] = [day([(BASE, BASE + timedelta(seconds=120), 0, 120)])]
    await login(client)
    body = await history(
        client,
        category="collection",
        **{"from": (BASE + timedelta(seconds=60)).isoformat(), "to": NOW.isoformat()},
    )
    assert body["page"]["totalItems"] == 1
    item = body["items"][0]
    assert item["id"].startswith("evt-auto-col-") and item["id"] != target
    assert item["recordingEventId"] == target and item["recordingId"] is None
    assert archive["requests"] == []
    response = await client.get(f"{API}/{target}")
    assert response.status_code == 200
    assert response.json()["event"]["windowState"] == "CLOSED"
    assert response.json()["playbackSeconds"] == 120
    assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_ordinary_links_and_real_clip_id_remain_distinct(client, test_db_session, archive):
    db = test_db_session
    rows = [
        AlertLog(pit_id="pit-demo", triggered_at=BASE, fill_ratio_percent=95, message="Synthetic"),
        CollectionLog(
            pit_id="pit-demo", completed_at=BASE, residual_height_cm=0, operator_name="Synthetic"
        ),
        ScrapMetric(pit_id="pit-demo", measured_at=BASE, is_valid=False),
        transition(
            "MEASUREMENT_VALIDITY_CHANGED", payload={"previousValid": True, "currentValid": False}
        ),
        transition("DEVICE_STATE_CHANGED", payload={"previous": "DEGRADED", "current": "HEALTHY"}),
    ]
    db.add_all(rows)
    await db.commit()
    clip_id = "00000000-0000-4000-8000-000000000001"
    db.add(
        MediaClipJob(
            event_id=f"evt-alt-{rows[0].id}",
            camera_id="camera-main",
            pit_id="pit-demo",
            category="alert",
            occurred_at=BASE,
            clip_id=clip_id,
        )
    )
    await db.commit()
    await login(client)
    body = await history(client)
    assert len(body["items"]) == 5
    assert archive["requests"] == []
    for item in body["items"]:
        assert item["recordingEventId"] == item["id"]
        assert item["recordingId"] == (clip_id if item["category"] == "alert" else None)
        response = await client.get(f"{API}/{item['recordingEventId']}")
        assert response.status_code == 200, response.text
        # A metadata link exists even when no archive contains the requested footage.
        assert response.json()["state"] == "UNAVAILABLE"
    assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 1


@pytest.mark.parametrize("unavailable", ["not-configured", "other-pit"])
async def test_history_succeeds_with_null_links_when_media_is_not_configured_for_pit(
    client, test_db_session, archive, monkeypatch, unavailable
):
    await cycle(test_db_session, end=BASE + timedelta(seconds=30))
    settings = get_settings()
    if unavailable == "not-configured":
        monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "")
        monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "")
    else:
        monkeypatch.setattr(settings, "MEDIA_PIT_ID", "other-pit")
    await login(client)
    body = await history(client)
    assert len(body["items"]) == 1
    assert body["items"][0]["recordingEventId"] is None
    assert archive["requests"] == []


@pytest.mark.parametrize(
    "mismatch",
    ["missing", "site", "edge", "pit", "scope", "detection", "after-end", "empty", "absent"],
)
async def test_unmatched_collection_has_no_invented_link(
    client, test_db_session, archive, mismatch
):
    db = test_db_session
    end_at = BASE + timedelta(seconds=30)
    end = transition("COLLECTION_SETTLED", end_at)
    start = transition("COLLECTION_DETECTED")
    if mismatch in ("site", "edge"):
        setattr(start, f"{mismatch}_id", "other")
    elif mismatch in ("pit", "scope", "detection"):
        key = {"pit": "pitId", "scope": "scopeToken", "detection": "detectionId"}[mismatch]
        start.payload[key] = "other"
    elif mismatch == "after-end":
        start.occurred_at = end_at + timedelta(seconds=1)
    elif mismatch == "empty":
        start.payload["detectionId"] = end.payload["detectionId"] = ""
    elif mismatch == "absent":
        del start.payload["scopeToken"]
        del end.payload["scopeToken"]
    db.add(end)
    if mismatch != "missing":
        db.add(start)
    await db.commit()
    await login(client)
    body = await history(client)
    assert len(body["items"]) == 1
    assert body["items"][0]["recordingEventId"] is None
    assert archive["requests"] == []


@pytest.mark.parametrize("duplicate", ["start", "end", "later-end"])
async def test_ambiguous_or_later_ends_do_not_link_to_another_playback_cycle(
    client, test_db_session, archive, duplicate
):
    db = test_db_session
    target = await cycle(db, end=BASE + timedelta(seconds=30))
    if duplicate == "start":
        row = transition("COLLECTION_DETECTED", BASE + timedelta(seconds=1))
    else:
        row = transition(
            "COLLECTION_SETTLED", BASE + timedelta(seconds=31 if duplicate == "later-end" else 30)
        )
    db.add(row)
    await db.commit()
    await login(client)
    body = await history(client)
    for item in body["items"]:
        expected = (
            target if duplicate == "later-end" and item["id"] != f"evt-auto-col-{row.id}" else None
        )
        assert item["recordingEventId"] == expected
    assert archive["requests"] == []


async def test_previous_scopes_match_their_own_start_and_do_not_borrow_current_source(
    client, test_db_session, archive, monkeypatch
):
    db = test_db_session
    old = await cycle(db, end=BASE + timedelta(seconds=10), scope="old")
    new = await cycle(
        db, start=BASE + timedelta(seconds=20), end=BASE + timedelta(seconds=30), scope="new"
    )
    await login(client)
    body = await history(client)
    assert [item["recordingEventId"] for item in body["items"]] == [new, old]
    # The default current-scope filter remains unchanged.
    assert (await history(client, collection_scope="current"))["items"] == []
    monkeypatch.setattr(get_settings(), "EDGE_V2_MONITORING_SOURCES", [])
    assert (await history(client))["items"] == []


async def test_future_manual_event_is_not_linked_and_legacy_events_need_no_edge_source(
    client, test_db_session, archive, monkeypatch
):
    db = test_db_session
    db.add_all(
        CollectionLog(
            pit_id="pit-demo", completed_at=at, residual_height_cm=0, operator_name="Synthetic"
        )
        for at in [BASE, datetime.now(UTC) + timedelta(days=1)]
    )
    await db.commit()
    monkeypatch.setattr(get_settings(), "EDGE_V2_MONITORING_SOURCES", [])
    await login(client)
    body = await history(client)
    assert body["items"][0]["recordingEventId"] is None
    assert body["items"][1]["recordingEventId"] == body["items"][1]["id"]
    assert archive["requests"] == []


async def test_page_enrichment_has_fixed_query_count_and_no_media_calls(
    client, test_db_session, test_engine, archive
):
    db = test_db_session
    db.add_all(
        CollectionLog(
            pit_id="pit-demo",
            completed_at=BASE + timedelta(seconds=index),
            residual_height_cm=0,
            operator_name="Synthetic",
        )
        for index in range(105)
    )
    await db.commit()
    await login(client)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sql_event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        first = await history(client, pageSize=1)
        one = len(statements)
        statements.clear()
        full = await history(client, pageSize=100)
        assert len(statements) == one
    finally:
        sql_event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert first["page"]["totalItems"] == full["page"]["totalItems"] == 105
    assert len(full["items"]) == 100
    assert all(item["recordingEventId"] == item["id"] for item in full["items"])
    remaining = await history(client, page=2, pageSize=100)
    assert len(remaining["items"]) == 5
    assert archive["requests"] == []
