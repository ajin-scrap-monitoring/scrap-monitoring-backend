"""Opt-in list coverage agrees with detail and remains bounded under media failures."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_daily_recordings import login
from test_event_recordings import API, BASE, NOW, alert, archive, cycle, day

from src.app.models.media_job import MediaClipJob
from src.app.schemas.event_recording import EventRecording
from src.app.services import event_playback_summary as summary

# Reuse the real HTTP archive fixture, including public path and identity validation.
__all__ = ["archive"]


def event(identifier, start=BASE, end=None):
    return EventRecording(
        eventId=identifier,
        pitId="pit-demo",
        category="collection",
        title="Synthetic cycle",
        eventType="COLLECTION_DETECTED",
        occurredAt=start,
        windowKind="COLLECTION_CYCLE",
        windowState="CLOSED",
        windowFrom=start,
        windowTo=end or start + timedelta(seconds=10),
    )


async def test_opt_in_authentication_default_and_no_jobs(client, test_db_session, archive):
    await alert(test_db_session)
    assert (await client.get(API, params={"include_playback": True})).status_code == 401
    assert archive["requests"] == []
    await login(client)
    body = (await client.get(API)).json()
    assert body["items"][0]["playback"] is None
    assert archive["requests"] == []
    assert (await client.get(API, params={"include_playback": "bad"})).status_code == 422
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_hundred_events_share_one_archive_query(client, test_db_session, archive):
    for n in range(100):
        await alert(test_db_session, BASE + timedelta(seconds=n))
    archive["days"] = [day([(BASE - timedelta(minutes=1), BASE + timedelta(minutes=5), 0, 360)])]
    await login(client)
    response = await client.get(API, params={"include_playback": True, "pageSize": 100})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert len(items) == 100 and len(archive["requests"]) == 1
    assert all(x["playback"]["state"] == "AVAILABLE" for x in items)
    assert all(x["playback"]["playbackSeconds"] == 60 for x in items)
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("mode", ["available", "partial", "missing", "future", "open", "point"])
async def test_list_matches_detail(client, test_db_session, archive, mode):
    start = BASE
    end = BASE + timedelta(seconds=10)
    if mode == "future":
        identifier = await alert(test_db_session, NOW - timedelta(seconds=10))
    else:
        identifier = await cycle(test_db_session, start=start, end=None if mode == "open" else end)
    intervals = [(BASE, end, 0, 10)]
    if mode == "partial":
        intervals = [(BASE, BASE + timedelta(seconds=5), 0, 5)]
    elif mode == "point":
        intervals = [(BASE, BASE, 0, 0.5)]
    if mode not in ("missing", "future"):
        archive["days"] = [day(intervals)]
    await login(client)
    params = {"include_playback": True, "to": (NOW + timedelta(minutes=1)).isoformat()}
    response = await client.get(API, params=params)
    assert response.status_code == 200, response.text
    actual = next(x["playback"] for x in response.json()["items"] if x["eventId"] == identifier)
    detail = (await client.get(f"{API}/{identifier}")).json()
    assert actual == {key: detail[key] for key in ("state", "playbackSeconds", "reasonCodes")}


async def test_media_error_preserves_list_and_does_not_claim_missing(
    client, test_db_session, archive
):
    await alert(test_db_session)
    archive["error"] = True
    await login(client)
    response = await client.get(API, params={"include_playback": True})
    assert response.status_code == 200
    assert response.json()["items"][0]["playback"] == {
        "state": "UNKNOWN",
        "playbackSeconds": None,
        "reasonCodes": ["STORAGE_UNAVAILABLE"],
    }
    assert "private-secret" not in response.text


async def test_korean_midnight_uses_shared_paginated_dates(client, test_db_session, archive):
    start = BASE.replace(hour=14, minute=59, second=55)
    end = start + timedelta(seconds=10)
    identifier = await cycle(test_db_session, start, end)
    midnight = start + timedelta(seconds=5)
    archive["days"] = [
        day([(start, midnight, 0, 5)]),
        day([(midnight, end, 0, 5)], date=end.astimezone(summary.KST).date()),
    ]
    await login(client)
    response = await client.get(API, params={"include_playback": True})
    assert response.status_code == 200, response.text
    assert len(archive["requests"]) == 2
    actual = next(x["playback"] for x in response.json()["items"] if x["eventId"] == identifier)
    assert actual["state"] == "AVAILABLE" and actual["playbackSeconds"] == 10


async def test_date_budget_and_long_window_do_not_break_page(archive):
    items = [event(str(n), BASE - timedelta(days=n)) for n in range(8)]
    items.append(event("long", BASE - timedelta(days=20), BASE))
    await summary.add_playback_summaries(items, NOW)
    assert all(x.playback.state == "UNAVAILABLE" for x in items[:7])
    assert all(x.playback.state == "UNKNOWN" for x in items[7:])
    assert len(archive["requests"]) == 1
    assert all(x.playback.playbackSeconds is None for x in items[7:])


async def test_timeout_cancels_fetch_and_returns_unknown(monkeypatch, archive):
    cancelled = []

    async def slow(params):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(summary, "SUMMARY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(summary.daily_recording_service, "list", slow)
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert cancelled == [True]
    assert items[0].playback.reasonCodes == ["PLAYBACK_SUMMARY_TIMEOUT"]
    assert items[0].playback.playbackSeconds is None


async def test_request_budget_discards_unfinished_pagination(monkeypatch, archive):
    archive["days"] = [
        day([(BASE, BASE + timedelta(seconds=10), 0, 10)]),
        day([], date=(BASE + timedelta(days=1)).astimezone(summary.KST).date()),
    ]
    monkeypatch.setattr(summary, "MAX_REQUESTS", 1)
    items = [event("one", BASE, BASE + timedelta(days=1))]
    await summary.add_playback_summaries(items, NOW)
    assert len(archive["requests"]) == 1
    assert items[0].playback.state == "UNKNOWN"
    assert items[0].playback.reasonCodes == ["PLAYBACK_SUMMARY_LIMIT"]


async def test_work_and_interval_budgets_are_unknown(monkeypatch, archive):
    archive["days"] = [day([(BASE, BASE + timedelta(seconds=10), 0, 10)])]
    monkeypatch.setattr(summary, "MAX_INTERVALS", 0)
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert items[0].playback.reasonCodes == ["PLAYBACK_SUMMARY_LIMIT"]
    monkeypatch.setattr(summary, "MAX_INTERVALS", 20_000)
    monkeypatch.setattr(summary, "MAX_RESOLUTION_WORK", 1)
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert items[0].playback.reasonCodes == ["PLAYBACK_SUMMARY_WORK_LIMIT"]


async def test_failed_later_range_does_not_invalidate_confirmed_missing(monkeypatch, archive):
    monkeypatch.setattr(summary, "MAX_REQUESTS", 1)
    items = [event("new"), event("old", BASE - timedelta(days=2))]
    await summary.add_playback_summaries(items, NOW)
    assert items[1].playback.state == "UNAVAILABLE"
    assert items[1].playback.playbackSeconds == 0
    assert items[0].playback.state == "UNKNOWN"


async def test_unknown_and_future_only_windows_do_not_query_media(archive):
    unknown = event("unknown")
    unknown.windowState = "UNKNOWN"
    unknown.reasonCodes = ["COLLECTION_END_UNKNOWN"]
    future = event("future", NOW + timedelta(seconds=10), NOW + timedelta(seconds=20))
    await summary.add_playback_summaries([unknown, future], NOW)
    assert archive["requests"] == []
    assert unknown.playback.state == "UNAVAILABLE"
    assert unknown.playback.reasonCodes == ["COLLECTION_END_UNKNOWN"]
    assert future.playback.state == "PROCESSING"
    assert future.playback.playbackSeconds == 0


async def test_repeated_cursor_is_unknown_and_empty_page_has_no_queries(archive):
    archive["repeat"] = True
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert items[0].playback.state == "UNKNOWN"
    assert items[0].playback.reasonCodes == ["MEDIA_BAD_RESPONSE"]
    archive["requests"].clear()
    await summary.add_playback_summaries([], NOW)
    assert archive["requests"] == []


async def test_summary_refreshes_after_deletion_and_invalid_identity(archive):
    recording = day([(BASE, BASE + timedelta(seconds=10), 0, 10)])
    archive["days"] = [recording]
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert items[0].playback.state == "AVAILABLE"
    recording["status"] = "deleted"
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert items[0].playback.state == "UNAVAILABLE"
    assert "DAILY_RECORDING_EXPIRED" in items[0].playback.reasonCodes
    recording["camera_id"] = "other-camera"
    items = [event("one")]
    await summary.add_playback_summaries(items, NOW)
    assert items[0].playback.state == "UNKNOWN"
    assert items[0].playback.reasonCodes == ["MEDIA_IDENTITY_MISMATCH"]


async def test_hundred_short_events_ignore_unrelated_daily_gaps(archive):
    intervals = [
        (
            BASE - timedelta(hours=3) + timedelta(seconds=2 * n),
            BASE - timedelta(hours=3) + timedelta(seconds=2 * n + 1),
            n,
            n + 1,
        )
        for n in range(300)
    ]
    intervals.append((BASE, BASE + timedelta(seconds=10), 300, 310))
    gaps = [
        (
            BASE - timedelta(hours=2) + timedelta(seconds=2 * n),
            BASE - timedelta(hours=2) + timedelta(seconds=2 * n + 1),
            "SYNTHETIC_GAP",
        )
        for n in range(1400)
    ]
    archive["days"] = [day(intervals, gaps)]
    items = [event(str(n)) for n in range(100)]
    await summary.add_playback_summaries(items, NOW)
    assert len(archive["requests"]) == 1
    assert all(x.playback.state == "AVAILABLE" for x in items)
    assert all(x.playback.playbackSeconds == 10 for x in items)
