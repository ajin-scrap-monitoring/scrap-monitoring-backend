"""Frozen event lists survive insertions, late commits and collection changes."""

from datetime import timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_event_recordings import API, BASE, NOW, alert, cycle
from test_event_recordings import archive as archive
from test_notification_reads import login

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.services import event_recording_service as live
from src.app.services import stable_event_pages as stable
from src.app.services.event_page_store import EventPageStore, PageContext


@pytest.fixture(autouse=True)
def stable_clock(archive, monkeypatch):
    monkeypatch.setattr(stable, "datetime", live.datetime)


def store(client):
    return client._transport.app.state.event_page_store


async def initial(client, db, **params):
    await login(client)
    for index in range(3):
        await alert(db, BASE - timedelta(minutes=index))
    response = await client.get(API, params={"stable": True, "pageSize": 2, **params})
    assert response.status_code == 200, response.text
    return response.json()


def continuation(body, **params):
    return {"snapshot_token": body["snapshotToken"], "pageSize": 2, "page": 2, **params}


@pytest.mark.parametrize("at", [BASE + timedelta(minutes=1), BASE - timedelta(seconds=30)])
async def test_new_and_late_events_do_not_shift_frozen_pages(
    client, test_db_session, test_engine, archive, at
):
    body = await initial(client, test_db_session)
    first_ids = [x["eventId"] for x in body["items"]]
    await alert(test_db_session, at)
    live_page = (await client.get(API, params={"page": 2, "pageSize": 2})).json()
    assert first_ids[-1] in [x["eventId"] for x in live_page["items"]]
    queries = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if " as event_id" in statement.lower():
            queries.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(API, params=continuation(body))
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 1 and page["page"]["totalItems"] == 3
    assert not set(first_ids) & {x["eventId"] for x in page["items"]}
    assert (
        page["asOf"] == body["asOf"] and page["from"] == body["from"] and page["to"] == body["to"]
    )
    assert page["snapshotExpiresAt"] == body["snapshotExpiresAt"]
    assert queries == [] and archive["requests"] == []
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("category", ["all", "collection", "alert", "error"])
async def test_stable_listing_preserves_live_sort_and_empty_categories(
    client, test_db_session, category
):
    body = await initial(client, test_db_session, category=category)
    current = await client.get(API, params={"category": category, "pageSize": 2})
    assert current.status_code == 200
    assert current.json()["items"] == body["items"]
    assert "snapshotToken" not in current.json() and "snapshotExpiresAt" not in current.json()
    repeated = await client.get(API, params=continuation(body, category=category, page=1))
    assert repeated.json() == body


async def test_collection_end_update_does_not_rewrite_frozen_metadata(client, test_db_session):
    db = test_db_session
    await login(client)
    identifier = await cycle(db)
    await alert(db, BASE + timedelta(minutes=1))
    first = await client.get(API, params={"stable": True, "pageSize": 1})
    body = first.json()
    frozen_before = (await client.get(API, params=continuation(body, pageSize=1))).json()
    assert frozen_before["items"][0]["eventId"] == identifier
    assert frozen_before["items"][0]["windowState"] == "UNKNOWN"
    start = await db.scalar(
        select(EdgeV2Transition).where(EdgeV2Transition.kind == "COLLECTION_DETECTED")
    )
    db.add(
        EdgeV2Transition(
            site_id=start.site_id,
            edge_id=start.edge_id,
            kind="COLLECTION_SETTLED",
            occurred_at=BASE + timedelta(seconds=30),
            payload=start.payload,
        )
    )
    await db.commit()
    frozen_after = await client.get(API, params=continuation(body, pageSize=1))
    assert frozen_after.json() == frozen_before
    fresh = (await client.get(API, params={"page": 2, "pageSize": 1})).json()
    assert fresh["items"][0]["windowState"] == "CLOSED"


async def test_newly_eligible_old_collection_does_not_enter_existing_snapshot(
    client, test_db_session
):
    db = test_db_session
    await login(client)
    await cycle(db, start=BASE - timedelta(days=1))
    await alert(db)
    params = {"from": BASE.isoformat(), "to": NOW.isoformat(), "pageSize": 2}
    first = (await client.get(API, params={**params, "stable": True})).json()
    assert first["page"]["totalItems"] == 1
    start = await db.scalar(select(EdgeV2Transition))
    db.add(
        EdgeV2Transition(
            site_id=start.site_id,
            edge_id=start.edge_id,
            kind="COLLECTION_SETTLED",
            occurred_at=BASE + timedelta(minutes=1),
            payload=start.payload,
        )
    )
    await db.commit()
    assert (await client.get(API, params=params)).json()["page"]["totalItems"] == 2
    frozen = await client.get(API, params=continuation(first, page=1))
    assert frozen.json() == first


@pytest.mark.parametrize(
    "changed",
    [
        {"category": "alert"},
        {"pageSize": 1},
        {"before_seconds": 20},
        {"from": BASE.isoformat()},
        {"to": BASE.isoformat()},
    ],
)
async def test_filter_changes_require_a_new_snapshot(client, test_db_session, changed):
    body = await initial(client, test_db_session)
    response = await client.get(API, params=continuation(body, **changed))
    assert response.status_code == 409
    assert response.json()["detail"] == "EVENT_LIST_QUERY_CHANGED"


async def test_source_changes_invalidate_snapshot(client, test_db_session, monkeypatch):
    body = await initial(client, test_db_session)
    monkeypatch.setattr(get_settings(), "MEDIA_CAMERA_ID", "synthetic-other-camera")
    response = await client.get(API, params=continuation(body))
    assert response.status_code == 409 and response.json()["detail"] == "EVENT_LIST_SOURCE_CHANGED"


async def test_snapshot_tokens_are_private_and_expire_or_disappear_on_restart(
    client, test_db_session
):
    cache = store(client)
    clock = [0.0]
    cache.clock = lambda: clock[0]
    body = await initial(client, test_db_session)
    await login(client, "operator")
    assert (await client.get(API, params=continuation(body))).status_code == 410
    await login(client)
    assert (await client.get(API, params=continuation(body))).status_code == 200
    clock[0] = cache.ttl
    assert (await client.get(API, params=continuation(body))).status_code == 410
    fresh = (await client.get(API, params={"stable": True, "pageSize": 2})).json()
    client._transport.app.state.event_page_store = EventPageStore()
    assert (await client.get(API, params=continuation(fresh))).status_code == 410


async def test_eviction_and_absent_large_pages_have_explicit_results(client, test_db_session):
    cache = store(client)
    cache.max_owner_entries = 1
    first = await initial(client, test_db_session)
    second = (await client.get(API, params={"stable": True, "pageSize": 2})).json()
    assert (await client.get(API, params=continuation(first))).status_code == 410
    huge = await client.get(API, params=continuation(second, page=9223372036854775808))
    assert huge.status_code == 200 and huge.json()["items"] == []
    assert huge.json()["page"]["totalItems"] == 3


@pytest.mark.parametrize("limit", ["MAX_ITEMS", "MAX_SNAPSHOT_BYTES"])
async def test_over_limit_queries_never_return_truncated_snapshots(
    client, test_db_session, monkeypatch, limit
):
    await login(client)
    for index in range(3):
        await alert(test_db_session, BASE - timedelta(minutes=index))
    monkeypatch.setattr(stable, limit, 2)
    response = await client.get(API, params={"stable": True})
    assert (
        response.status_code == 422 and response.json()["detail"] == "EVENT_LIST_SNAPSHOT_TOO_LARGE"
    )
    assert len(store(client).entries) == 0 and store(client).builds == 0


async def test_busy_snapshots_and_bad_tokens_do_not_start_new_queries(client):
    await login(client)
    cache = store(client)
    cache.builds = 2
    assert (await client.get(API, params={"stable": True})).status_code == 503
    cache.builds = 0
    assert (await client.get(API, params={"stable": True, "page": 2})).status_code == 422
    assert (await client.get(API, params={"snapshot_token": "bad"})).status_code == 422
    assert (await client.get(API, params={"snapshot_token": "f" * 32})).status_code == 410
    assert not cache.entries


async def test_playback_summary_does_not_mutate_frozen_event_items(
    client, test_db_session, archive
):
    body = await initial(client, test_db_session)
    summary = await client.get(API, params=continuation(body, page=1, include_playback=True))
    assert summary.status_code == 200
    assert all(x["playback"] is not None for x in summary.json()["items"])
    plain = await client.get(API, params=continuation(body, page=1))
    assert plain.json() == body
    assert all(r.method == "GET" for r in archive["requests"])


async def test_late_lower_id_commit_is_excluded_from_the_frozen_list(client, test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Independent concurrent transactions require PostgreSQL")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    await login(client)
    async with factory() as delayed, factory() as visible:
        late = AlertLog(
            pit_id="pit-demo",
            triggered_at=BASE,
            fill_ratio_percent=95,
            message="Synthetic late commit",
        )
        delayed.add(late)
        await delayed.flush()
        for index in (1, 2):
            visible.add(
                AlertLog(
                    pit_id="pit-demo",
                    triggered_at=BASE - timedelta(minutes=index),
                    fill_ratio_percent=95,
                    message="Synthetic visible",
                )
            )
        await visible.commit()
        first = await client.get(API, params={"stable": True, "pageSize": 1})
        assert first.status_code == 200
        body = first.json()
        assert body["page"]["totalItems"] == 2
        await delayed.commit()
        second = await client.get(API, params=continuation(body, pageSize=1))
        assert second.status_code == 200 and second.json()["page"]["totalItems"] == 2
        ids = {body["items"][0]["eventId"], second.json()["items"][0]["eventId"]}
        assert len(ids) == 2 and f"evt-alt-{late.id}" not in ids


def test_store_capacity_byte_budget_and_owner_checks():
    cache = EventPageStore(max_bytes=2200)
    context = PageContext("pit-demo", "source", "all", 1, 30, 30, BASE, NOW, NOW)
    first = cache.put("a", context, [b"x" * 100], NOW)
    second = cache.put("b", context, [b"x" * 100], NOW)
    assert cache.size <= 2200 and len(cache.entries) == 1
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        cache.get(first.token, "a")
    with pytest.raises(HTTPException):
        cache.get(second.token, "a")
    assert cache.get(second.token, "b") == second


async def test_equal_timestamps_keep_lexical_order_across_pages(client, test_db_session):
    await login(client)
    for _ in range(12):
        await alert(test_db_session, BASE)
    first = (await client.get(API, params={"stable": True, "pageSize": 4})).json()
    items = list(first["items"])
    for page in (2, 3):
        response = await client.get(API, params=continuation(first, page=page, pageSize=4))
        assert response.status_code == 200
        items.extend(response.json()["items"])
    ids = [item["eventId"] for item in items]
    assert len(set(ids)) == 12 and ids == sorted(ids, reverse=True)


async def test_interrupted_stream_closes_cursor_without_partial_cache(
    client, test_db_session, monkeypatch
):
    await login(client)
    await alert(test_db_session, BASE)
    original = test_db_session.stream
    closed = []

    class Interrupted:
        def __init__(self, result):
            self.result = result

        async def close(self):
            await self.result.close()
            closed.append(True)

        async def rows(self):
            async for row in self.result.mappings():
                yield row
                raise TimeoutError

        def mappings(self):
            return self.rows()

    async def interrupted(*args, **kwargs):
        return Interrupted(await original(*args, **kwargs))

    monkeypatch.setattr(test_db_session, "stream", interrupted)
    response = await client.get(API, params={"stable": True})
    assert response.status_code == 503
    assert response.json()["detail"] == "EVENT_LIST_QUERY_TIMEOUT"
    assert closed == [True] and not store(client).entries and store(client).builds == 0
