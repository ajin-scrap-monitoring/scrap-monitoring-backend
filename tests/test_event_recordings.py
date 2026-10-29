"""Shared archive replay, wall-clock gaps, and absence of media mutations."""

from datetime import UTC, datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from test_daily_recordings import login, recording

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.event_recording_queries import event_table
from src.app.services.media_client import media_client

BASE = datetime(2026, 10, 14, 3, tzinfo=UTC)
NOW = BASE + timedelta(days=1)
API = "/api/v2/recordings/events"
KST = ZoneInfo("Asia/Seoul")


def day(intervals, gaps=(), *, date=None, now=NOW):
    date = date or BASE.astimezone(KST).date()
    start = datetime.combine(date, time(), KST).astimezone(UTC)
    end = start + timedelta(days=1)
    item = recording()
    identifier = str(uuid5(NAMESPACE_URL, date.isoformat()))
    item.update(
        recording_id=identifier,
        recording_date=date.isoformat(),
        window_start_at=start.isoformat(),
        window_end_at=end.isoformat(),
        status="closed" if now >= end else "recording",
        coverage="partial" if now >= end else None,
        coverage_as_of_at=min(now, end).isoformat(),
        playable_until_at=max((b for a, b, c, d in intervals), default=None),
        recorded_seconds=sum(d - c for a, b, c, d in intervals),
        gaps=[
            {"start_at": a.isoformat(), "end_at": b.isoformat(), "reason_code": reason}
            for a, b, reason in gaps
        ],
        available_intervals=[
            {
                "start_at": a.isoformat(),
                "end_at": b.isoformat(),
                "playback_start_seconds": c,
                "playback_end_seconds": d,
            }
            for a, b, c, d in intervals
        ],
        playback_state="ready" if intervals else "unavailable",
        playback_protocol="hls" if intervals else None,
        playback_path=f"/api/v2/daily-recordings/{identifier}/hls/index.m3u8"
        if intervals
        else None,
        has_gaps=bool(gaps),
        error=None,
    )
    if item["playable_until_at"] is not None:
        item["playable_until_at"] = item["playable_until_at"].isoformat()
    return item


@pytest.fixture
async def archive(monkeypatch):
    clock = {"now": NOW}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.event_recording_service.datetime", Clock)
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "http://daily.test/api/v2")
    monkeypatch.setattr(settings, "MEDIA_PIT_ID", "pit-demo")
    monkeypatch.setattr(settings, "MEDIA_CAMERA_ID", "camera-main")
    monkeypatch.setattr(
        settings,
        "EDGE_V2_MONITORING_SOURCES",
        [MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")],
    )
    data = {"days": [], "requests": [], "clock": clock, "error": None, "repeat": False}

    async def handle(request):
        data["requests"].append(request)
        assert (
            request.method == "GET"
            and "authorization" not in request.headers
            and "cookie" not in request.headers
        )
        if data["error"]:
            return httpx.Response(
                503, json={"error": {"code": "STORAGE_UNAVAILABLE", "message": "private-secret"}}
            )
        query = request.url.params
        items = sorted(
            (
                x
                for x in data["days"]
                if query["from_date"] <= x["recording_date"] < query["to_date"]
            ),
            key=lambda x: x["recording_date"],
            reverse=True,
        )
        position = int(query.get("cursor", "0"))
        count = 1
        next_cursor = str(position + count) if position + count < len(items) else None
        if data["repeat"]:
            next_cursor = "0"
        return httpx.Response(
            200, json={"items": items[position : position + count], "next_cursor": next_cursor}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as mock:
        monkeypatch.setattr(media_client, "_client", mock)
        yield data


async def alert(db, at=BASE):
    row = AlertLog(
        pit_id="pit-demo",
        triggered_at=at,
        fill_ratio_percent=95,
        channel="LOG",
        status="RECORDED",
        message="Synthetic",
    )
    db.add(row)
    await db.commit()
    return f"evt-alt-{row.id}"


async def cycle(db, start=BASE, end=None, scope="synthetic-scope"):
    payload = {
        "pitId": "pit-demo",
        "detectionId": "synthetic-cycle",
        "scopeToken": scope,
        "confirmedCompletion": False,
        "simulated": True,
    }
    row = EdgeV2Transition(
        site_id="site-demo",
        edge_id="edge-demo",
        kind="COLLECTION_DETECTED",
        occurred_at=start,
        payload=payload,
    )
    db.add(row)
    if end is not None:
        db.add(
            EdgeV2Transition(
                site_id="site-demo",
                edge_id="edge-demo",
                kind="COLLECTION_SETTLED",
                occurred_at=end,
                payload=payload,
            )
        )
    await db.commit()
    return f"evt-auto-col-start-{row.id}"


async def test_authentication_and_read_only_metadata_listing(client, test_db_session, archive):
    db = test_db_session
    identifier = await alert(db)
    assert (await client.get(API)).status_code == 401
    await login(client)
    response = await client.get(API, params={"category": "alert"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["eventId"] == identifier
    assert archive["requests"] == []
    assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_collection_reuses_archive_and_returns_exact_seek_positions(
    client, test_db_session, archive
):
    identifier = await cycle(test_db_session, end=BASE + timedelta(seconds=120))
    archive["days"] = [day([(BASE - timedelta(seconds=20), BASE + timedelta(seconds=150), 0, 170)])]
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "AVAILABLE" and body["coveredSeconds"] == 120
    assert body["parts"][0]["startSeconds"] == 20 and body["parts"][0]["endSeconds"] == 140
    assert body["parts"][0]["playbackPath"].startswith("/media/daily-recordings/")
    assert body["event"]["simulated"] is True
    assert (
        body["downloadPath"] is None
        and body["downloadReasonCode"] == "EVENT_RANGE_EXPORT_UNSUPPORTED"
    )
    assert len(archive["requests"]) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_ten_second_event_uses_zero_based_time_in_a_late_started_seventeen_hour_archive(
    client, test_db_session, archive
):
    midnight = datetime.combine(BASE.astimezone(KST).date(), time(), KST).astimezone(UTC)
    recording_start = midnight + timedelta(hours=7)
    recording_end = midnight + timedelta(days=1)
    event_start = midnight + timedelta(hours=14, minutes=13, seconds=20)
    identifier = await cycle(test_db_session, event_start, event_start + timedelta(seconds=10))
    archive["days"] = [day([(recording_start, recording_end, 0, 17 * 3600)])]
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "AVAILABLE"
    assert body["requestedSeconds"] == body["playbackSeconds"] == 10
    assert len(body["parts"]) == 1
    part = body["parts"][0]
    assert (part["startSeconds"], part["endSeconds"]) == (26000, 26010)
    assert (part["relativeStartSeconds"], part["relativeEndSeconds"]) == (0, 10)
    assert part["fromAt"] == event_start.isoformat().replace("+00:00", "Z")
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_hundreds_of_events_page_without_loss_and_share_one_daily_recording(
    client, test_db_session, archive
):
    midnight = datetime.combine(BASE.astimezone(KST).date(), time(), KST).astimezone(UTC)
    recording_start = midnight + timedelta(hours=7)
    archive["days"] = [day([(recording_start, midnight + timedelta(days=1), 0, 17 * 3600)])]
    expected = {"collection": set(), "alert": set(), "error": set()}
    models = []
    for index in range(123):
        start = recording_start + timedelta(seconds=(index // 2) * 800)
        payload = {
            "pitId": "pit-demo",
            "detectionId": f"synthetic-{index}",
            "scopeToken": "synthetic-pagination",
            "simulated": True,
        }
        common = {"site_id": "site-demo", "edge_id": "edge-demo"}
        detected = EdgeV2Transition(
            **common, kind="COLLECTION_DETECTED", occurred_at=start, payload=payload
        )
        settled = EdgeV2Transition(
            **common,
            kind="COLLECTION_SETTLED",
            occurred_at=start + timedelta(seconds=10),
            payload=payload,
        )
        alarm = AlertLog(
            pit_id="pit-demo",
            triggered_at=start + timedelta(seconds=40),
            fill_ratio_percent=95,
            channel="LOG",
            status="RECORDED",
            message="Synthetic",
        )
        fault = EdgeV2Transition(
            **common,
            kind="DEVICE_STATE_CHANGED",
            occurred_at=start + timedelta(seconds=80),
            payload={
                "pitId": "pit-demo",
                "previous": "HEALTHY",
                "current": "MEASUREMENT_UNAVAILABLE",
                "simulated": True,
            },
        )
        test_db_session.add_all([detected, settled, alarm, fault])
        models.append((detected, alarm, fault))
    await test_db_session.flush()
    for detected, alarm, fault in models:
        expected["collection"].add(f"evt-auto-col-start-{detected.id}")
        expected["alert"].add(f"evt-alt-{alarm.id}")
        expected["error"].add(f"evt-v2-transition-{fault.id}")
    await test_db_session.commit()
    expected["all"] = set.union(*expected.values())
    await login(client)
    selected = []
    for category, identifiers in expected.items():
        received = []
        pages = (len(identifiers) + 99) // 100
        params = {
            "from": midnight.isoformat(),
            "to": (midnight + timedelta(days=1)).isoformat(),
            "category": category,
            "pageSize": 100,
        }
        for page in range(1, pages + 2):
            response = await client.get(API, params={**params, "page": page})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["page"]["totalItems"] == len(identifiers)
            assert body["page"]["totalPages"] == pages
            received.extend(item["eventId"] for item in body["items"])
            if page == 1 and category != "all":
                selected.append(body["items"][0]["eventId"])
            if page > pages:
                assert body["items"] == []
        assert len(received) == len(set(received)) == len(identifiers)
        assert set(received) == identifiers
    assert archive["requests"] == []  # Listing never creates or fetches media.
    recording_ids = set()
    for identifier in selected:
        response = await client.get(f"{API}/{identifier}")
        assert response.status_code == 200, response.text
        replay = response.json()
        assert replay["state"] == "AVAILABLE"
        assert replay["parts"][0]["relativeStartSeconds"] == 0
        assert replay["parts"][-1]["relativeEndSeconds"] == replay["playbackSeconds"]
        recording_ids.update(part["recordingId"] for part in replay["parts"])
    assert len(recording_ids) == 1
    assert len(archive["requests"]) == 3
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_gap_after_event_start_does_not_seek_as_if_wall_time_were_playback_time(
    client, test_db_session, archive
):
    identifier = await cycle(test_db_session, end=BASE + timedelta(seconds=60))
    archive["days"] = [
        day(
            [
                (BASE, BASE + timedelta(seconds=20), 0, 20),
                (BASE + timedelta(seconds=30), BASE + timedelta(seconds=60), 20, 50),
            ],
            [(BASE + timedelta(seconds=20), BASE + timedelta(seconds=30), "CAMERA_DISCONNECTED")],
        )
    ]
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["state"] == "PARTIAL" and body["coveredSeconds"] == body["playbackSeconds"] == 50
    assert body["parts"][1]["startSeconds"] == 20
    assert [(p["relativeStartSeconds"], p["relativeEndSeconds"]) for p in body["parts"]] == [
        (0, 20),
        (20, 50),
    ]
    assert body["unavailableIntervals"][0]["reasonCode"] == "CAMERA_DISCONNECTED"


async def test_alert_context_is_configurable_and_mapping_can_have_different_time_scales(
    client, test_db_session, archive
):
    identifier = await alert(test_db_session, BASE + timedelta(seconds=40))
    archive["days"] = [day([(BASE, BASE + timedelta(seconds=100), 0, 50)])]
    await login(client)
    body = (
        await client.get(f"{API}/{identifier}", params={"before_seconds": 10, "after_seconds": 20})
    ).json()
    assert body["state"] == "AVAILABLE" and body["requestedSeconds"] == 30
    assert body["parts"][0]["startSeconds"] == 15 and body["parts"][0]["endSeconds"] == 30


async def test_korean_midnight_uses_two_existing_recordings(client, test_db_session, archive):
    midnight = BASE.replace(hour=15, minute=0, second=0)
    start, end = midnight - timedelta(seconds=5), midnight + timedelta(seconds=5)
    identifier = await cycle(test_db_session, start, end)
    archive["days"] = [
        day(
            [(midnight - timedelta(seconds=20), midnight, 0, 20)], date=start.astimezone(KST).date()
        ),
        day([(midnight, midnight + timedelta(seconds=20), 0, 20)], date=end.astimezone(KST).date()),
    ]
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "AVAILABLE" and body["coveredSeconds"] == 10 and len(body["parts"]) == 2
    assert [p["startSeconds"] for p in body["parts"]] == [15, 0]
    assert [(p["relativeStartSeconds"], p["relativeEndSeconds"]) for p in body["parts"]] == [
        (0, 5),
        (5, 10),
    ]
    assert len({p["recordingId"] for p in body["parts"]}) == 2
    assert len(archive["requests"]) == 2  # The fixture deliberately returns one date per page.


async def test_exact_midnight_end_does_not_request_the_next_date(client, test_db_session, archive):
    end = BASE.replace(hour=15)
    start = end - timedelta(seconds=10)
    identifier = await cycle(test_db_session, start, end)
    archive["days"] = [day([(start, end, 0, 10)], date=start.astimezone(KST).date())]
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["state"] == "AVAILABLE"
    assert archive["requests"][0].url.params["to_date"] == end.astimezone(KST).date().isoformat()


async def test_storage_full_returns_unavailable_without_inventing_footage(
    client, test_db_session, archive
):
    identifier = await cycle(test_db_session, end=BASE + timedelta(seconds=60))
    missing = day([], [(BASE, BASE + timedelta(seconds=60), "STORAGE_FULL")])
    missing["error"] = {"code": "INSUFFICIENT_STORAGE", "message": "private-secret"}
    archive["days"] = [missing]
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "UNAVAILABLE" and body["parts"] == [] and body["coveredSeconds"] == 0
    assert "STORAGE_FULL" in body["reasonCodes"] and "private-secret" not in response.text


async def test_missing_archive_and_unknown_collection_end_stay_distinct(
    client, test_db_session, archive
):
    closed = await cycle(test_db_session, end=BASE + timedelta(seconds=60))
    await login(client)
    body = (await client.get(f"{API}/{closed}")).json()
    assert body["state"] == "UNAVAILABLE" and "DAILY_RECORDING_NOT_FOUND" in body["reasonCodes"]
    row = EdgeV2Transition(
        site_id="site-demo",
        edge_id="edge-demo",
        occurred_at=BASE,
        kind="COLLECTION_DETECTED",
        payload={"pitId": "pit-demo", "scopeToken": "other", "detectionId": "unfinished"},
    )
    test_db_session.add(row)
    await test_db_session.commit()
    archive["requests"].clear()
    body = (await client.get(f"{API}/evt-auto-col-start-{row.id}")).json()
    assert body["state"] == "UNAVAILABLE" and "COLLECTION_END_NOT_RECORDED" in body["reasonCodes"]
    assert archive["requests"] == []


async def test_point_timestamp_interval_keeps_its_playback_span_without_fake_wall_duration(
    client, test_db_session, archive
):
    identifier = await alert(test_db_session, BASE)
    archive["days"] = [day([(BASE, BASE, 0, 2)])]
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert (
        body["state"] == "PARTIAL" and body["coveredSeconds"] == 0 and body["playbackSeconds"] == 2
    )
    assert body["parts"][0]["mapping"] == "POINT_TIMESTAMP"
    assert (body["parts"][0]["relativeStartSeconds"], body["parts"][0]["relativeEndSeconds"]) == (
        0,
        2,
    )
    assert body["parts"][0]["fromAt"] == body["parts"][0]["toAt"]


async def test_conflicting_gap_metadata_cannot_claim_complete_replay(
    client, test_db_session, archive
):
    identifier = await cycle(test_db_session, end=BASE + timedelta(seconds=60))
    archive["days"] = [
        day(
            [(BASE, BASE + timedelta(seconds=60), 0, 60)],
            [(BASE + timedelta(seconds=20), BASE + timedelta(seconds=30), "STORAGE_FULL")],
        )
    ]
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["state"] == "UNAVAILABLE" and "COVERAGE_METADATA_CONFLICT" in body["reasonCodes"]


async def test_categories_ties_pagination_and_utc_offsets(client, test_db_session, archive):
    db = test_db_session
    await cycle(db, BASE, BASE + timedelta(seconds=120))
    await alert(db)
    db.add(
        CollectionLog(
            pit_id="pit-demo", completed_at=BASE, residual_height_cm=0, operator_name="Synthetic"
        )
    )
    db.add(
        ScrapMetric(
            pit_id="pit-demo",
            measurement_id="synthetic-invalid",
            measured_at=BASE,
            lidar1_distance_cm=0,
            calculated_height_cm=0,
            fill_ratio_percent=0,
            is_valid=False,
        )
    )
    for kind, payload in [
        ("DEVICE_STATE_CHANGED", {"previous": "HEALTHY", "current": "MEASUREMENT_UNAVAILABLE"}),
        (
            "MEASUREMENT_VALIDITY_CHANGED",
            {"previousValid": True, "currentValid": False, "reasonCodes": ["MEDIAN_WARMING"]},
        ),
    ]:
        db.add(
            EdgeV2Transition(
                site_id="site-demo",
                edge_id="edge-demo",
                kind=kind,
                occurred_at=BASE,
                payload={"pitId": "pit-demo", **payload},
            )
        )
    await db.commit()
    await login(client)
    result = (await client.get(API)).json()
    assert result["page"]["totalItems"] == 6, result
    ids = []
    for page in range(1, 4):
        response = await client.get(API, params={"page": page, "pageSize": 2})
        assert response.status_code == 200, response.text
        ids.extend(i["eventId"] for i in response.json()["items"])
    assert len(set(ids)) == 6 and ids == [i["eventId"] for i in result["items"]]
    for category, count in [("collection", 2), ("alert", 1), ("error", 3)]:
        body = (await client.get(API, params={"category": category})).json()
        assert body["page"]["totalItems"] == count and all(
            x["category"] == category for x in body["items"]
        )
    lo, hi = BASE + timedelta(seconds=40), BASE + timedelta(seconds=50)
    a = (await client.get(API, params={"from": lo.isoformat(), "to": hi.isoformat()})).json()
    b = (
        await client.get(
            API,
            params={"from": lo.astimezone(KST).isoformat(), "to": hi.astimezone(KST).isoformat()},
        )
    ).json()
    assert (
        a["items"] == b["items"] and len(a["items"]) == 1
    )  # Collection overlaps even though it starts earlier.
    assert archive["requests"] == []


@pytest.mark.parametrize(
    "query",
    [
        {"from": "2026-10-15T00:00:00Z", "to": "2026-10-14T00:00:00Z"},
        {"from": "2026-10-14T00:00:00"},
        {"before_seconds": 0, "after_seconds": 0},
        {"before_seconds": 301},
        {"pageSize": 101},
        {"from": "2025-01-01T00:00:00Z"},
    ],
)
async def test_invalid_ranges_are_rejected(client, archive, query):
    await login(client)
    assert (await client.get(API, params=query)).status_code == 422
    assert archive["requests"] == []


async def test_wrong_pit_unknown_id_and_upstream_error_are_safe(client, test_db_session, archive):
    identifier = await alert(test_db_session)
    await login(client)
    assert (await client.get(API, params={"pit_id": "other-pit"})).status_code == 404
    assert (await client.get(f"{API}/evt-alt-999999")).status_code == 404
    archive["error"] = True
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 503 and "private-secret" not in response.text


async def test_repeated_archive_cursor_is_rejected(client, test_db_session, archive):
    identifier = await alert(test_db_session)
    archive["repeat"] = True
    archive["days"] = [day([(BASE, BASE + timedelta(seconds=60), 0, 60)])]
    await login(client)
    assert (await client.get(f"{API}/{identifier}")).status_code == 502


async def test_future_tail_is_pending_not_a_recording_gap(client, test_db_session, archive):
    archive["clock"]["now"] = BASE + timedelta(seconds=5)
    identifier = await alert(test_db_session, BASE)
    archive["days"] = [
        day(
            [(BASE - timedelta(seconds=10), BASE + timedelta(seconds=5), 0, 15)],
            now=archive["clock"]["now"],
        )
    ]
    await login(client)
    body = (
        await client.get(f"{API}/{identifier}", params={"before_seconds": 10, "after_seconds": 10})
    ).json()
    assert body["state"] == "PARTIAL" and body["coveredSeconds"] == 15
    assert [i["kind"] for i in body["unavailableIntervals"]] == ["FUTURE"]


async def test_confirmed_active_collection_stays_open(
    client, test_db_session, archive, monkeypatch
):
    from src.app.models.edge_v2_projection import EdgeV2Latest
    from src.app.schemas.collection_detection import CollectionTracker
    from src.app.services.collection_detection_service import TRACKER_KIND

    archive["clock"]["now"] = BASE + timedelta(seconds=120)
    identifier = await cycle(test_db_session)
    monkeypatch.setattr(get_settings(), "EDGE_V2_COLLECTION_DETECTION_ENABLED", True)
    monkeypatch.setattr(
        "src.app.services.event_recording_queries.collection_scope",
        lambda source, pit: "synthetic-scope",
    )
    state = CollectionTracker(
        pit_id="pit-demo",
        scope_token="synthetic-scope",
        phase="COLLECTING",
        episode_id="synthetic-cycle",
        observed_at=archive["clock"]["now"],
    )
    test_db_session.add(
        EdgeV2Latest(
            site_id="site-demo",
            edge_id="edge-demo",
            kind=TRACKER_KIND,
            message_id="synthetic",
            generated_at=archive["clock"]["now"],
            payload=state.model_dump(mode="json"),
        )
    )
    await test_db_session.commit()
    archive["days"] = [
        day([(BASE, BASE + timedelta(seconds=100), 0, 100)], now=BASE + timedelta(seconds=100))
    ]
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event"]["windowState"] == "OPEN" and body["event"]["windowTo"] is None
    assert body["state"] == "PARTIAL" and "COLLECTION_IN_PROGRESS" in body["reasonCodes"]
    archive["clock"]["now"] += timedelta(seconds=4)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["event"]["windowState"] == "UNKNOWN" and body["parts"] == []


async def test_mismatched_collection_end_and_other_source_cannot_supply_a_window(
    client, test_db_session, archive
):
    identifier = await cycle(test_db_session)
    for site, scope in [("other-site", "synthetic-scope"), ("site-demo", "other-scope")]:
        test_db_session.add(
            EdgeV2Transition(
                site_id=site,
                edge_id="edge-demo",
                kind="COLLECTION_SETTLED",
                occurred_at=BASE + timedelta(seconds=60),
                payload={
                    "pitId": "pit-demo",
                    "scopeToken": scope,
                    "detectionId": "synthetic-cycle",
                },
            )
        )
    await test_db_session.commit()
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["event"]["eventTo"] is None and body["state"] == "UNAVAILABLE"
    assert archive["requests"] == []


async def test_expired_archive_never_exposes_a_stale_playback_link(
    client, test_db_session, archive
):
    identifier = await alert(test_db_session)
    item = day([(BASE - timedelta(seconds=30), BASE + timedelta(seconds=30), 0, 60)])
    item["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    archive["days"] = [item]
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["state"] == "UNAVAILABLE" and body["parts"] == []
    assert body["unavailableIntervals"][0]["kind"] == "EXPIRED"


async def test_large_event_window_is_bounded(client, test_db_session, archive):
    identifier = await cycle(test_db_session, BASE - timedelta(days=8), BASE)
    await login(client)
    assert (await client.get(f"{API}/{identifier}")).status_code == 422
    assert archive["requests"] == []


@pytest.mark.parametrize(
    "prefix", ["evt-alt", "evt-col", "evt-auto-col-start", "evt-v2-transition", "evt-err"]
)
@pytest.mark.parametrize("number", [2147483648, 9223372036854775807])
async def test_replay_event_number_outside_database_range_is_not_found(
    client, test_db_session, archive, prefix, number
):
    await login(client)
    response = await client.get(f"{API}/{prefix}-{number}")
    assert response.status_code == 404
    assert response.json()["detail"] == "EVENT_NOT_FOUND"
    assert archive["requests"] == []
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_replay_accepts_largest_database_event_number(client, test_db_session, archive):
    await alert(test_db_session)
    row = await test_db_session.scalar(select(AlertLog))
    row.id = 2147483647
    await test_db_session.commit()
    await login(client)
    response = await client.get(f"{API}/evt-alt-2147483647")
    assert response.status_code == 200
    assert response.json()["event"]["eventId"] == "evt-alt-2147483647"


async def test_event_batch_ignores_numbers_outside_database_range(test_db_session):
    identifier = await alert(test_db_session)
    table = event_table(
        test_db_session,
        "pit-demo",
        None,
        "alert",
        None,
        None,
        NOW,
        30,
        30,
        None,
        only_ids={identifier, "evt-alt-2147483648", "evt-alt-9223372036854775807"},
    )
    rows = (await test_db_session.execute(select(table))).mappings().all()
    assert [row["event_id"] for row in rows] == [identifier]
