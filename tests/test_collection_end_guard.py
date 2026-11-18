"""Public invented signals: independent decline guards and durable episode cooldown."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.app.core.config import Settings, get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.collection_detection import CollectionTracker
from src.app.services.collection_detection_service import (
    TRACKER_KIND,
    advance,
    invalidate,
    process_collection_source,
)
from src.app.services.monitoring_v2_service import monitoring_source
from tests import test_simulation_height as simulation
from tests.test_collection_detection import detector_frame, event_rows
from tests.test_collection_detection import enabled_detector as enabled_detector
from tests.test_collection_profiles import timing as timing
from tests.test_height_fill_pipeline import START
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_sensor_angle_height import angle_context as angle_context


def state():
    return CollectionTracker(pit_id="pit-demo", scope_token="invented", observed_at=START)


def test_upper_plateau_does_not_end_a_continuing_mean_descent(timing):
    tracker = state()
    events = []
    for second in range(175):
        upper = 800 if second < 20 else 200 if second < 100 else 100
        mean = 600 if second < 20 else max(0, 200 - 2 * (second - 20))
        events.extend(
            (second, kind, detail)
            for kind, detail in advance(
                tracker,
                upper,
                START + timedelta(seconds=second),
                settle_seconds=20,
                representative_cm=mean,
            )
        )
    assert [kind for _, kind, _ in events] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert events[1][0] >= 140  # Mean descent lasts through second 120.
    assert events[0][2]["detectionId"] == events[1][2]["detectionId"]


def test_mean_decline_cannot_start_collection_without_upper_drop(timing):
    tracker = state()
    for second in range(200):
        assert (
            advance(
                tracker,
                800,
                START + timedelta(seconds=second),
                settle_seconds=20,
                representative_cm=max(0, 600 - second * 3),
            )
            == []
        )


def test_mean_noise_and_transient_dip_do_not_hold_end_forever(timing):
    tracker = state()
    events = []
    for second in range(71):
        upper = 800 if second < 20 else 200
        mean = 600 if second < 20 else 150 + second % 2
        if second == 45:
            mean = 100  # A single invented bad sample does not confirm a decline.
        events.extend(
            (second, kind)
            for kind, _ in advance(
                tracker, upper, START + timedelta(seconds=second), 20, representative_cm=mean
            )
        )
    assert events == [(30, "COLLECTION_DETECTED"), (50, "COLLECTION_SETTLED")]


def test_outage_restarts_both_quiet_guards_without_new_episode(timing):
    tracker = state()
    for second in range(31):
        advance(tracker, 800 if second < 20 else 200, START + timedelta(seconds=second))
    identity = tracker.episode_id
    invalidate(tracker)
    assert tracker.representative_anchor_cm is None
    assert tracker.representative_candidate_since is None
    restored = CollectionTracker.model_validate_json(tracker.model_dump_json())
    events = []
    for second in range(1000, 1021):
        events.extend(
            (second, detail)
            for _, detail in advance(
                restored, 200, START + timedelta(seconds=second), 20, representative_cm=100
            )
        )
    assert len(events) == 1 and events[0][0] == 1020
    assert events[0][1]["detectionId"] == identity


def test_cooldown_survives_serialization_and_allows_new_work_after_expiry(timing):
    tracker = state()
    tracker.phase, tracker.baseline_cm = "IDLE", 800
    tracker.last_settled_at = START
    for second in range(1, 600):
        if second == 100:
            tracker = CollectionTracker.model_validate_json(tracker.model_dump_json())
            invalidate(tracker)
        assert advance(tracker, 100, START + timedelta(seconds=second), 20) == []
        assert tracker.candidate_since is None
    events = []
    for second in range(600, 651):
        height = 800 if second < 620 else 100
        events.extend(
            (second, kind)
            for kind, _ in advance(tracker, height, START + timedelta(seconds=second), 20)
        )
    assert events == [(630, "COLLECTION_DETECTED"), (650, "COLLECTION_SETTLED")]
    assert tracker.last_settled_at == START + timedelta(seconds=650)


def test_cooldown_default_and_bounds():
    assert Settings(_env_file=None).EDGE_V2_COLLECTION_COOLDOWN_SECONDS == 600
    assert Settings(_env_file=None, EDGE_V2_COLLECTION_COOLDOWN_SECONDS=0)
    for value in [-1, 86401]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, EDGE_V2_COLLECTION_COOLDOWN_SECONDS=value)


def test_continuing_decline_can_keep_one_episode_for_three_hours(timing):
    tracker = state()
    events = []
    stop = 3 * 3600
    for second in range(stop + 61):
        elapsed = min(max(0, second - 20), stop - 20)
        upper = 20000 if second < 20 else 19000 - 10 * (elapsed // 60)
        mean = 18000 if second < 20 else 18000 - elapsed
        events.extend(
            (second, kind)
            for kind, _ in advance(
                tracker, upper, START + timedelta(seconds=second), 20, representative_cm=mean
            )
        )
    assert [kind for _, kind in events] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    assert events[1][0] >= stop + 20


def test_explicit_zero_cooldown_allows_a_new_confirmed_drop(timing, monkeypatch):
    monkeypatch.setenv("EDGE_V2_COLLECTION_COOLDOWN_SECONDS", "0")
    get_settings.cache_clear()
    tracker = state()
    tracker.phase, tracker.baseline_cm, tracker.last_settled_at = "IDLE", 800, START
    events = []
    for second in range(1, 31):
        events.extend(
            kind
            for kind, _ in advance(
                tracker, 800 if second < 20 else 100, START + timedelta(seconds=second), 20
            )
        )
    assert events == ["COLLECTION_DETECTED"]


async def test_legacy_tracker_seeds_cooldown_from_end_and_keeps_it_across_scope_change(
    client, test_db_session, enabled_detector, monkeypatch
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    ended_at = START - timedelta(seconds=2)
    db.add(
        EdgeV2Transition(
            site_id=source.site_id,
            edge_id=source.edge_id,
            kind="COLLECTION_SETTLED",
            occurred_at=ended_at,
            payload={"pitId": source.pit_id, "scopeToken": "earlier-settings"},
        )
    )
    await db.commit()
    for second in range(35):
        await detector_frame(client, db, enabled_detector, second, 80 if second < 10 else 40)
    assert len(await event_rows(db)) == 1
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    assert row is not None
    before = CollectionTracker.model_validate(row.payload)
    assert before.last_settled_at == ended_at and before.cooldown_initialized
    monkeypatch.setenv("EDGE_V2_COLLECTION_CONFIRM_SECONDS", "3")
    get_settings.cache_clear()
    for second in range(35, 65):
        await detector_frame(client, db, enabled_detector, second, 20)
    assert len(await event_rows(db)) == 1
    after = CollectionTracker.model_validate(row.payload)
    assert after.scope_token != before.scope_token
    assert after.last_settled_at == ended_at and after.cooldown_initialized


async def test_failed_settlement_commit_does_not_consume_cooldown_or_duplicate_end(
    client, test_db_session, enabled_detector, monkeypatch
):
    db = test_db_session
    await simulation.prepare(client, db)
    source = monitoring_source("pit-demo")
    for second in range(17):
        await detector_frame(client, db, enabled_detector, second, 80 if second < 10 else 40)
    assert [r.kind for r in await event_rows(db)] == ["COLLECTION_DETECTED"]
    await simulation.frame(client, db, enabled_detector, 17, 40)
    commit = db.commit

    async def fail_commit():
        raise RuntimeError("Invented end commit failure")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="Invented"):
        await process_collection_source(db, source, START + timedelta(seconds=17))
    await db.rollback()
    monkeypatch.setattr(db, "commit", commit)
    assert [r.kind for r in await event_rows(db)] == ["COLLECTION_DETECTED"]
    await process_collection_source(db, source, START + timedelta(seconds=17))
    await process_collection_source(db, source, START + timedelta(seconds=17))
    assert [r.kind for r in await event_rows(db)] == ["COLLECTION_DETECTED", "COLLECTION_SETTLED"]
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND))
    assert CollectionTracker.model_validate(row.payload).last_settled_at == START + timedelta(
        seconds=17
    )
