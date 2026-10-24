"""Invented height sequences; no site noise, geometry or field criteria are assumed."""

import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from src.app.core.config import Settings, get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.models.log import AlertLog, CollectionLog
from src.app.schemas.collection_detection import CollectionTracker
from src.app.services.collection_detection_service import (
    TRACKER_KIND,
    advance,
    collection_scope,
    invalidate,
    process_collection_source,
)
from src.app.services.collection_summary_v2_service import collection_summary
from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
from src.app.services.monitoring_v2_service import monitoring_source
from tests import test_simulation_height as simulation
from tests.test_height_fill_pipeline import START
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_lidar_height import REFERENCE as VERIFIED_REFERENCE
from tests.test_monitoring_v2 import login
from tests.test_sensor_angle_height import angle_context as angle_context


@pytest.fixture
def detector_config(monkeypatch):
    monkeypatch.setenv("EDGE_V2_COLLECTION_DROP_METERS", "3")
    monkeypatch.setenv("EDGE_V2_COLLECTION_STABLE_BAND_METERS", ".1")
    monkeypatch.setenv("EDGE_V2_COLLECTION_BASELINE_SECONDS", "3")
    monkeypatch.setenv("EDGE_V2_COLLECTION_CONFIRM_SECONDS", "3")
    monkeypatch.setenv("EDGE_V2_COLLECTION_SETTLE_SECONDS", "5")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def tracker():
    return CollectionTracker(pit_id="pit-demo", scope_token="toy-scope", observed_at=START)


def feed(state, heights, start=0):
    events = []
    for second, height in enumerate(heights, start):
        events.extend(advance(state, height, START + timedelta(seconds=second)))
    return events


def test_gradual_cumulative_drop_and_one_episode_for_many_grabs(detector_config):
    state = tracker()
    assert feed(state, [1000] * 4) == []
    assert state.phase == "IDLE"
    # Each individual reduction is small; the fixed stable baseline retains their sum.
    events = feed(state, [950, 900, 850, 800, 750, 700, 680, 660, 640, 620, 600], 4)
    assert [kind for kind, _ in events] == ["COLLECTION_DETECTED"]
    assert events[0][1]["dropCm"] == 360
    assert events[0][1]["confirmedCompletion"] is False
    identity = state.episode_id
    assert feed(state, [580, 560, 540, 520], 15) == []
    # The final further drop must first persist, then the quiet interval can elapse.
    settled = feed(state, [520] * 7, 19)
    assert [kind for kind, _ in settled] == ["COLLECTION_SETTLED"]
    assert settled[0][1]["detectionId"] == identity
    assert settled[0][1]["confirmedCompletion"] is False
    assert state.phase == "IDLE"
    # The residual height becomes the next baseline, preventing immediate rediscovery.
    assert feed(state, [520] * 6, 26) == []


def test_short_dip_and_unstable_baseline_do_not_trigger(detector_config):
    state = tracker()
    feed(state, [1000] * 4)
    assert feed(state, [650, 650, 1000, 1000], 4) == []
    noisy = tracker()
    assert feed(noisy, [1000, 950] * 20) == []
    assert noisy.phase == "BASELINING"


def test_new_stable_loading_level_raises_baseline(detector_config):
    state = tracker()
    feed(state, [500] * 4)
    feed(state, [1000] * 4, 4)
    assert state.baseline_cm == 1000
    assert len(feed(state, [650] * 4, 8)) == 1


def test_outage_resets_candidate_and_cannot_finish_an_active_episode(detector_config):
    state = tracker()
    feed(state, [1000] * 4)
    feed(state, [650, 650], 4)
    assert state.phase == "CANDIDATE"
    invalidate(state)
    assert feed(state, [650] * 4, 100) == []
    assert state.baseline_cm == 650
    feed(state, [300] * 4, 104)
    identity = state.episode_id
    invalidate(state)
    assert state.phase == "COLLECTING"
    assert feed(state, [300] * 5, 1000) == []
    events = feed(state, [300], 1005)
    assert events[0][1]["detectionId"] == identity


@pytest.mark.parametrize("drop", [0, -1, "nan", "inf"])
def test_invalid_meter_settings_rejected(drop):
    with pytest.raises(ValidationError):
        Settings(EDGE_V2_COLLECTION_DROP_METERS=drop)


def test_detector_is_opt_in_and_cannot_enable_without_explicit_drop():
    assert Settings().EDGE_V2_COLLECTION_DETECTION_ENABLED is False
    assert Settings().EDGE_V2_COLLECTION_DROP_METERS is None
    with pytest.raises(ValidationError):
        Settings(EDGE_V2_COLLECTION_DETECTION_ENABLED=True)


@pytest.fixture
def enabled_detector(angle_context, monkeypatch):
    # Small public toy box: 0.2m = 20cm, unrelated to a proposed field threshold.
    monkeypatch.setenv("EDGE_V2_COLLECTION_DROP_METERS", ".2")
    monkeypatch.setenv("EDGE_V2_COLLECTION_STABLE_BAND_METERS", ".02")
    monkeypatch.setenv("EDGE_V2_COLLECTION_BASELINE_SECONDS", "2")
    monkeypatch.setenv("EDGE_V2_COLLECTION_CONFIRM_SECONDS", "2")
    monkeypatch.setenv("EDGE_V2_COLLECTION_SETTLE_SECONDS", "3")
    monkeypatch.setenv("EDGE_V2_COLLECTION_SIMULATION_SETTLE_SECONDS", "3")
    monkeypatch.setenv("EDGE_V2_COLLECTION_DETECTION_ENABLED", "true")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            now = angle_context["now"]
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.collection_summary_v2_service.datetime", Clock)
    monkeypatch.setattr("src.app.services.collection_ended_service.datetime", Clock)
    get_settings.cache_clear()
    yield angle_context
    get_settings.cache_clear()


async def detector_frame(client, db, clock, second, height, failed=False):
    _, snapshot = await simulation.frame(client, db, clock, second, height, sensor_failure=failed)
    await EdgeV2OperationsWorker().run_once(db, START + timedelta(seconds=second))
    return snapshot


async def event_rows(db):
    return (
        await db.scalars(
            select(EdgeV2Transition)
            .where(EdgeV2Transition.kind.in_(["COLLECTION_DETECTED", "COLLECTION_SETTLED"]))
            .order_by(EdgeV2Transition.id)
        )
    ).all()


async def test_worker_persists_detection_summary_and_history_without_confirmed_completion(
    client, test_db_session, enabled_detector
):
    db = test_db_session
    await simulation.prepare(client, db)
    await login(client)
    heights = [80] * 10 + [75, 70, 65, 60, 55, 50, 45, 40] + [40] * 12
    collecting_checked = False
    for second, height in enumerate(heights):
        await detector_frame(client, db, enabled_detector, second, height)
        if [row.kind for row in await event_rows(db)] == ["COLLECTION_DETECTED"]:
            collecting_checked = True
            response = await client.get("/api/v1/events?pit_id=pit-demo&category=collection")
            assert response.json()["items"] == []
            pending = await collection_summary(
                db, monitoring_source("pit-demo"), now=START + timedelta(seconds=second)
            )
            assert pending.lastCollectionActivityAt is None
            assert pending.detectionState == "COLLECTING"
    assert collecting_checked
    rows = await event_rows(db)
    assert [row.kind for row in rows] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert rows[0].payload["detectionId"] == rows[1].payload["detectionId"]
    assert all(row.payload["simulated"] for row in rows)
    assert all(row.payload["confirmedCompletion"] is False for row in rows)
    assert await db.scalar(select(func.count()).select_from(CollectionLog)) == 0
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0
    source = monitoring_source("pit-demo")
    now = START + timedelta(seconds=len(heights) - 1)
    result = await collection_summary(db, source, now=now)
    assert result.lastCollectionAt is None
    assert result.lastCollectionActivityAt == result.lastSettledCollectionAt
    assert result.lastSettledCollectionAt > result.lastDetectedCollectionAt
    assert result.activitySource == "HEIGHT_SETTLED"
    assert result.detectedCollectionsInWindow == 1
    assert result.secondsSinceLastCollectionActivity > 0
    assert result.detectionDropMeters == 0.2
    assert result.detectionState == "IDLE"
    assert result.averageDetectedCollectionCycleSeconds is None
    # Restart and duplicate reads cannot replay a detection or finish a second time.
    before = (await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))).payload
    await EdgeV2OperationsWorker().run_once(db, now)
    await process_collection_source(db, source, now)
    assert len(await event_rows(db)) == 2
    assert (
        await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    ).payload == before
    legacy = await client.get("/api/v1/events?pit_id=pit-demo&category=collection&pageSize=1")
    assert legacy.status_code == 200
    ended = legacy.json()["items"][0]
    assert legacy.json()["page"]["totalItems"] == 1
    # These are the exact fields validated by the deployed dashboard's existing reader.
    assert (ended["type"], ended["category"], ended["status"]) == (
        "collection_completed",
        "collection",
        "completed",
    )
    assert datetime.fromisoformat(ended["occurredAt"]) == result.lastSettledCollectionAt
    assert ended["collectionSource"] == "HEIGHT_SETTLED"
    assert ended["confirmedCompletion"] is False
    assert ended["simulated"] is True
    assert ended["detectionId"] == rows[1].payload["detectionId"]
    assert ended["recordingId"] is None
    assert result.secondsSinceLastSettlement == int(
        (now - datetime.fromisoformat(ended["occurredAt"])).total_seconds()
    )
    response = await client.get("/api/v2/monitoring/collection-detections?pit_id=pit-demo")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["totalItems"] == 2
    summary = await client.get("/api/v2/monitoring/collection-summary?pit_id=pit-demo")
    assert summary.json()["activitySource"] == "HEIGHT_SETTLED"


async def test_height_drop_after_sensor_outage_is_not_a_new_collection(
    client, test_db_session, enabled_detector
):
    db = test_db_session
    await simulation.prepare(client, db)
    for second in range(10):
        await detector_frame(client, db, enabled_detector, second, 80)
    await detector_frame(client, db, enabled_detector, 10, 20, failed=True)
    for second in range(11, 25):
        await detector_frame(client, db, enabled_detector, second, 20)
    assert await event_rows(db) == []


async def test_detection_history_requires_authentication_and_source(client, enabled_detector):
    url = "/api/v2/monitoring/collection-detections?pit_id=pit-demo"
    assert (await client.get(url)).status_code == 401
    await login(client)
    assert (await client.get(url.replace("pit-demo", "other"))).status_code == 404
    assert (await client.get(url + "&limit=101")).status_code == 422
    assert (await client.get(url)).json()["items"] == []


async def test_enabled_but_unverified_height_basis_reports_unknown(
    client, test_db_session, enabled_detector, monkeypatch
):
    monkeypatch.setenv("EDGE_V2_SIMULATION_HEIGHT_REFERENCES", "[]")
    monkeypatch.setenv(
        "EDGE_V2_HEIGHT_REFERENCES", json.dumps([{**VERIFIED_REFERENCE, "verified": False}])
    )
    get_settings.cache_clear()
    source = monitoring_source("pit-demo")
    result = await collection_summary(test_db_session, source, now=START)
    assert result.detectionEnabled
    assert result.detectionState == "UNKNOWN"
    assert result.detectionReasonCodes == ["DETECTION_NOT_CONFIGURED"]


async def test_detection_summary_scope_time_average_and_manual_completion_are_distinct(
    client, test_db_session, enabled_detector
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    scope = collection_scope(source, await db.get(PitConfig, "pit-demo"))

    def event(second, kind="COLLECTION_DETECTED", **overrides):
        return EdgeV2Transition(
            site_id=source.site_id,
            edge_id=source.edge_id,
            occurred_at=START + timedelta(seconds=second),
            kind=kind,
            payload={"pitId": source.pit_id, "scopeToken": scope, **overrides},
        )

    db.add_all(
        [
            event(0),
            event(10),
            event(20),
            event(5, kind="COLLECTION_SETTLED"),
            event(15, kind="COLLECTION_SETTLED"),
            event(40),
            event(25, scopeToken="other"),
            event(26, pitId="other-pit"),
        ]
    )
    await db.commit()
    summary = await collection_summary(db, source, now=START + timedelta(seconds=30))
    assert summary.detectedCollectionsInWindow == 3
    assert summary.averageDetectedCollectionCycleSeconds == 10
    assert summary.activitySource == "HEIGHT_SETTLED"
    assert summary.secondsSinceLastCollectionActivity == 15
    assert summary.completedCollectionsInWindow == 0
    db.add(
        CollectionLog(
            pit_id=source.pit_id,
            completed_at=START + timedelta(seconds=25),
            residual_height_cm=10,
            operator_name="Synthetic operator",
        )
    )
    await db.commit()
    result = await collection_summary(db, source, now=START + timedelta(seconds=30))
    assert result.activitySource == "CONFIRMED_COMPLETION"
    assert result.secondsSinceLastCollectionActivity == 5
    assert result.detectedCollectionsInWindow == 3
    assert result.completedCollectionsInWindow == 1


async def test_failed_commit_retries_detection_without_duplicates(
    client, test_db_session, enabled_detector, monkeypatch
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    for second in range(14):
        await detector_frame(client, db, enabled_detector, second, 80 if second < 10 else 40)
    assert await event_rows(db) == []
    await simulation.frame(client, db, enabled_detector, 14, 40)
    commit = db.commit

    async def fail_commit():
        raise RuntimeError("Synthetic commit failure")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await process_collection_source(db, source, START + timedelta(seconds=14))
    await db.rollback()
    monkeypatch.setattr(db, "commit", commit)
    assert await event_rows(db) == []
    await process_collection_source(db, source, START + timedelta(seconds=14))
    assert len(await event_rows(db)) == 1
    await process_collection_source(db, source, START + timedelta(seconds=14))
    assert len(await event_rows(db)) == 1


async def test_legacy_elapsed_keeps_last_end_during_next_collection_and_filters_scope(
    client, test_db_session, enabled_detector
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    scope = collection_scope(source, await db.get(PitConfig, source.pit_id))
    now = START + timedelta(seconds=30)
    enabled_detector["now"] = now

    def ended(second, kind="COLLECTION_SETTLED", site=None, edge=None, **payload):
        return EdgeV2Transition(
            site_id=site or source.site_id,
            edge_id=edge or source.edge_id,
            occurred_at=START + timedelta(seconds=second),
            kind=kind,
            payload={
                "pitId": source.pit_id,
                "scopeToken": scope,
                "detectionId": f"toy-{second}",
                "simulated": True,
                "confirmedCompletion": False,
                **payload,
            },
        )

    db.add_all(
        [
            ended(0),
            ended(10),
            ended(20, kind="COLLECTION_DETECTED"),
            ended(25, pitId="other"),
            ended(26, scopeToken="old-criteria"),
            ended(27, site="other"),
            ended(28, edge="other"),
            ended(40),
        ]
    )
    await db.commit()
    url = "/api/v1/events?pit_id=pit-demo&category=collection&pageSize=1"
    await login(client)
    data = (await client.get(url)).json()
    assert data["page"]["totalItems"] == 2
    assert data["page"]["totalPages"] == 2
    assert datetime.fromisoformat(data["items"][0]["occurredAt"]) == START + timedelta(seconds=10)
    assert (await client.get(url + "&page=2")).json()["items"][0]["detectionId"] == "toy-0"
    result = await collection_summary(db, source, now=now)
    assert result.lastDetectedCollectionAt == START + timedelta(seconds=20)
    assert result.secondsSinceLastCollectionActivity == 20
    assert result.secondsSinceLastSettlement == 20
    bounded = await client.get(
        url + "&from=" + (START + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    )
    assert bounded.json()["page"]["totalItems"] == 1
    assert (await client.get(url.replace("pit-demo", "unconfigured"))).json()["items"] == []
    assert (await client.get(url.replace("category=collection", "category=alert"))).json()[
        "items"
    ] == []
    # A later end, rather than the start above, advances the display exactly once.
    db.add(ended(29))
    await db.commit()
    result = await collection_summary(db, source, now=now)
    assert result.secondsSinceLastCollectionActivity == 1
    data = (await client.get(url)).json()
    assert data["page"]["totalItems"] == 3
    assert data["items"][0]["detectionId"] == "toy-29"
    db.add(
        CollectionLog(
            pit_id=source.pit_id,
            completed_at=now,
            residual_height_cm=10,
            operator_name="Synthetic operator",
        )
    )
    await db.commit()
    latest = (await client.get(url)).json()["items"][0]
    assert latest["confirmedCompletion"] is True
    assert latest["collectionSource"] == "MANUAL_CONFIRMATION"
    assert latest["id"].startswith("evt-col-")


async def test_settled_cycle_mean_excludes_starts_duplicates_other_scopes_and_manual_records(
    client, test_db_session, enabled_detector
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    scope = collection_scope(source, await db.get(PitConfig, source.pit_id))

    def event(second, kind="COLLECTION_SETTLED", site=None, edge=None, **changes):
        return EdgeV2Transition(
            site_id=site or source.site_id,
            edge_id=edge or source.edge_id,
            occurred_at=START + timedelta(seconds=second),
            kind=kind,
            payload={"pitId": source.pit_id, "scopeToken": scope, **changes},
        )

    db.add_all(
        [
            event(-86400),
            event(0),
            event(5),
            event(15),
            event(15),
            event(40),
            event(1, kind="COLLECTION_DETECTED"),
            event(2, kind="COLLECTION_DETECTED"),
            event(20, scopeToken="old-scope"),
            event(21, pitId="other-pit"),
            event(22, site="other-site"),
            event(23, edge="other-edge"),
            CollectionLog(
                pit_id=source.pit_id,
                completed_at=START + timedelta(seconds=12),
                residual_height_cm=1,
                operator_name="Synthetic",
            ),
        ]
    )
    await db.commit()
    now = START + timedelta(seconds=30)
    data = await collection_summary(db, source, window_days=1, now=now)
    assert data.settledCollectionsInWindow == 3
    assert data.settledCycleCount == 2
    assert data.averageSettledCollectionCycleSeconds == 7.5
    assert data.settledCycleReasonCodes == []
    assert data.averageDetectedCollectionCycleSeconds == 1
    assert data.completedCollectionsInWindow == 1
    assert data.averageCollectionCycleSeconds is None
    enabled_detector["now"] = now
    await login(client)
    body = (
        await client.get("/api/v2/monitoring/collection-summary?pit_id=pit-demo&window_days=1")
    ).json()
    assert body["averageSettledCollectionCycleSeconds"] == 7.5
    assert body["settledCollectionsInWindow"] == 3


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_settled_cycle_requires_two_ends_and_includes_window_boundaries(
    client, test_db_session, enabled_detector, count
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    scope = collection_scope(source, await db.get(PitConfig, source.pit_id))
    for seconds in [0, 86400][:count]:
        db.add(
            EdgeV2Transition(
                site_id=source.site_id,
                edge_id=source.edge_id,
                occurred_at=START + timedelta(seconds=seconds),
                kind="COLLECTION_SETTLED",
                payload={"pitId": source.pit_id, "scopeToken": scope},
            )
        )
    await db.commit()
    data = await collection_summary(db, source, window_days=1, now=START + timedelta(days=1))
    assert data.settledCollectionsInWindow == count
    assert data.settledCycleCount == max(0, count - 1)
    if count == 2:
        assert data.averageSettledCollectionCycleSeconds == 86400
        assert data.settledCycleReasonCodes == []
    else:
        assert data.averageSettledCollectionCycleSeconds is None
        assert data.settledCycleReasonCodes


@pytest.mark.parametrize("phase", ["CANDIDATE", "COLLECTING", "UNKNOWN"])
async def test_forecast_hides_future_arrival_during_collection_or_unknown_tracker(
    client, test_db_session, enabled_detector, phase
):
    from src.app.services.fill_forecast_service import fill_forecast

    db = test_db_session
    await simulation.prepare(client, db)
    for second in range(5):
        _, snapshot = await simulation.frame(client, db, enabled_detector, second, 50)
    source = monitoring_source("pit-demo")
    now = enabled_detector["now"]
    scope = collection_scope(source, await db.get(PitConfig, source.pit_id))
    state = CollectionTracker(
        pit_id=source.pit_id,
        scope_token=scope,
        observed_at=now,
        phase=phase if phase != "UNKNOWN" else "IDLE",
        input_valid=phase != "UNKNOWN",
    )
    db.add(
        EdgeV2Latest(
            site_id=source.site_id,
            edge_id=source.edge_id,
            kind=TRACKER_KIND,
            message_id=snapshot.fillObservation.message_id,
            generated_at=now,
            payload=state.model_dump(mode="json"),
        )
    )
    await db.commit()
    data = await fill_forecast(db, source, now)
    assert data.simulated and data.inputSource == "HEIGHT_SECTIONS"
    assert data.state == "UNAVAILABLE" and data.expectedThresholdAt is None
    assert data.reasonCodes == [
        "COLLECTION_STATE_UNAVAILABLE" if phase == "UNKNOWN" else "COLLECTION_IN_PROGRESS"
    ]
