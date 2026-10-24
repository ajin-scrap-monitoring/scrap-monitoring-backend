"""Synthetic operational scenarios: bounded points, spike rejection and durable minute history."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import get_settings
from src.app.db.migration import migration_config, require_current_schema, upgrade_schema
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2Operation
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest, EdgeV2Minute
from src.app.models.log import AlertLog
from src.app.services.edge_v2_history import collection_events, state_events
from src.app.services.edge_v2_operations import process_source
from src.app.services.edge_v2_service import device_status
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from tests.test_edge_v2 import measurement
from tests.test_monitoring_v2 import DEVICE, SOURCE, TOKEN, assessment, login, status

START = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def compact_config(monkeypatch):
    clock = {"now": START}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "true")
    monkeypatch.setattr("src.app.services.edge_v2_service.datetime", Clock)
    get_settings.cache_clear()
    yield clock
    get_settings.cache_clear()


async def send(client, payload, clock, at=None):
    clock["now"] = at or datetime.fromisoformat(payload["generated_at"])
    route = {
        "lidar_measurement": "measurements",
        "edge_assessment": "assessments",
        "edge_status": "status",
    }[payload["message_type"]]
    return await client.post(
        "/api/edge/v2/" + route,
        json=payload,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Idempotency-Key": payload["message_id"],
        },
    )


async def healthy(client, clock, when=START, identifier="status-1", sequence=1):
    r = await send(client, status(identifier, when, sequence), clock)
    assert r.status_code == 201, r.text


async def sample(client, db, clock, second, percent, operate=False):
    when = START + timedelta(seconds=second)
    payload = assessment(f"state-{second}", percent / 100, when)
    r = await send(client, payload, clock)
    assert r.status_code == 201, r.text
    source = monitoring_source("pit-demo")
    if operate:
        await process_source(db, source, when)
    return await build_snapshot(db, source, when)


def scan_message(second, count=1000):
    payload = measurement(f"scan-message-{second}", count)
    when = START + timedelta(seconds=second)
    payload["generated_at"] = when.isoformat()
    payload["config_revision"] = "cfg-demo"
    scan = payload["sensors"][0]["scan"]
    scan["scan_id"] = f"scan-{second}"
    scan["sequence"] = second
    scan["acquired_at"] = when.isoformat()
    return payload


async def test_scan_points_stay_bounded_but_old_receipts_and_conflicts_survive(
    client,
    test_db_session,
    compact_config,
):
    first = scan_message(0)
    ack = await send(client, first, compact_config)
    assert ack.status_code == 201
    for second in range(1, 15):
        assert (await send(client, scan_message(second), compact_config)).status_code == 201
    db = test_db_session
    assert await db.scalar(select(func.count()).select_from(EdgeV2Latest)) == 1
    latest = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "measurement"))
    assert latest.payload["sensors"][0]["scan"]["scan_id"] == "scan-14"
    receipts = (await db.scalars(select(EdgeV2Message))).all()
    artifacts = (await db.scalars(select(EdgeV2Artifact))).all()
    assert len(receipts) == 15
    assert all("points" not in json.dumps(r.payload) for r in receipts)
    assert all("points" not in json.dumps(a.payload) for a in artifacts)
    assert sum(len(json.dumps(r.payload)) for r in receipts) < len(json.dumps(first))
    replay = await send(client, first, compact_config, START + timedelta(seconds=15))
    assert replay.status_code == 200 and replay.json()["duplicate"]
    assert replay.json()["received_at"] == ack.json()["received_at"]
    changed = copy.deepcopy(first)
    changed["sensors"][0]["scan"]["points"][0]["distance_mm"] += 1
    assert (await send(client, changed, compact_config)).status_code == 409
    changed["message_id"] = "new-message-same-conflicting-scan"
    assert (await send(client, changed, compact_config)).status_code == 409
    assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 15


async def test_one_spike_does_not_create_collection_alert(client, test_db_session, compact_config):
    await healthy(client, compact_config)
    for second, value in enumerate([70, 71, 96, 72, 71]):
        result = await sample(client, test_db_session, compact_config, second, value, True)
        if second < 4:
            assert result.summary.loadPercent is None
            assert result.summary.filterState == "WARMING"
    assert result.summary.filterState == "READY"
    assert result.summary.loadPercent == 71
    assert result.assessment.fill_estimate.ratio == 0.71
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 0


async def test_true_rise_is_confirmed_and_hysteresis_cooldown_survive(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(5):
        await sample(client, test_db_session, compact_config, second, 70, True)
    for second in range(5, 12):
        await sample(client, test_db_session, compact_config, second, 95, True)
        count = await test_db_session.scalar(select(func.count()).select_from(AlertLog))
        assert count == (0 if second < 10 else 1)
    row = await test_db_session.get(EdgeV2Operation, "pit-demo")
    assert row.state == "CRITICAL"
    for second in range(12, 17):
        await sample(client, test_db_session, compact_config, second, 88, True)
    assert row.state == "CRITICAL"
    for second in range(17, 22):
        await sample(client, test_db_session, compact_config, second, 84, True)
    assert row.state == "WARNING"
    for second in range(22, 29):
        await sample(client, test_db_session, compact_config, second, 96, True)
    assert row.state == "CRITICAL"
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 1


async def test_unavailable_status_resets_filter_and_pending_alarm(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(5):
        await sample(client, test_db_session, compact_config, second, 95, True)
    row = await test_db_session.get(EdgeV2Operation, "pit-demo")
    assert row.critical_since is not None
    bad = status("status-bad", START + timedelta(seconds=5), 2)
    bad["overall_state"] = "MEASUREMENT_UNAVAILABLE"
    assert (await send(client, bad, compact_config)).status_code == 201
    await process_source(
        test_db_session, monitoring_source("pit-demo"), START + timedelta(seconds=5)
    )
    assert row.critical_since is None
    await healthy(client, compact_config, START + timedelta(seconds=6), "status-recovery", 3)
    for second in range(6, 10):
        result = await sample(client, test_db_session, compact_config, second, 95, True)
        assert result.summary.loadPercent is None
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 0


async def test_invalid_assessment_resets_window_and_never_becomes_zero(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(5):
        await sample(client, test_db_session, compact_config, second, 70)
    invalid = assessment("invalid-state", 0.7, START + timedelta(seconds=5))
    invalid.update(fill_estimate=None, decision_state="UNAVAILABLE")
    assert (await send(client, invalid, compact_config)).status_code == 201
    result = await build_snapshot(
        test_db_session, monitoring_source("pit-demo"), START + timedelta(seconds=5)
    )
    assert result.summary.loadPercent is None
    assert result.summary.filterState == "UNAVAILABLE"
    for second in range(6, 10):
        result = await sample(client, test_db_session, compact_config, second, 70)
        assert result.summary.loadPercent is None


async def test_replays_same_second_and_delayed_evidence_do_not_fabricate_filter_samples(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for offset in range(7):
        when = START + timedelta(milliseconds=offset * 100)
        payload = assessment(f"rapid-{offset}", 0.7, when)
        assert (await send(client, payload, compact_config)).status_code == 201
    row = await test_db_session.get(EdgeV2Filter, ("site-demo", "edge-demo"))
    # Observation-aligned slots may straddle a center inside one UTC second, but a
    # subsecond burst still cannot prepare the five-slot window or supply a value.
    before_replay = copy.deepcopy(row.payload)
    assert len(row.payload["samples"]) < 5 and row.payload["value"] is None
    assert row.payload["state"] == "WARMING"
    assert (await send(client, payload, compact_config)).status_code == 200
    late = assessment("late-state", 0.99, START - timedelta(seconds=1))
    assert (await send(client, late, compact_config, START)).status_code == 201
    assert row.payload == before_replay
    assert (await send(client, scan_message(2), compact_config)).status_code == 201
    old = scan_message(1)
    old.update(
        delivery_kind="EVIDENCE",
        report_interval_ms=None,
        selection_policy="REFERENCED_SCANS",
        pair_state="NOT_APPLICABLE",
        assessment_ids=["historical-event"],
    )
    old["sensors"] = old["sensors"][:1]
    assert (
        await send(client, old, compact_config, START + timedelta(seconds=3))
    ).status_code == 201
    latest = await test_db_session.get(EdgeV2Latest, ("site-demo", "edge-demo", "measurement"))
    assert latest.message_id == "scan-message-2"


async def test_state_filter_stays_ready_for_distinct_scans_across_utc_boundary(
    client, test_db_session, compact_config
):
    await healthy(client, compact_config)
    for index in range(20):
        second = index + 0.999 + (0.003 if index == 8 else 0)
        snapshot = await sample(client, test_db_session, compact_config, second, 70)
        if index >= 4:
            assert snapshot.dataState == "FRESH"
            assert snapshot.summary.filterState == "READY"
            assert snapshot.summary.loadPercent == 70


async def test_minute_summaries_survive_session_restart_and_retain_quality(
    client,
    test_db_session,
    test_engine,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(10):
        await sample(client, test_db_session, compact_config, second, 70 + second)
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Minute)) == 1
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        result = await load_history(
            db,
            monitoring_source("pit-demo"),
            START,
            START + timedelta(minutes=1),
            60,
            START + timedelta(minutes=1),
        )
        point = result.samples[0]
        assert point.valuePolicy == "MEDIAN_MINUTE"
        assert point.valuePercent == 77
        assert point.minimumPercent == 72 and point.maximumPercent == 77
        assert point.averagePercent == 74.5
        assert point.validSamples == 6 and point.invalidSamples == 4
        assert point.assessmentId == "state-9"
        expired = await build_snapshot(
            db, monitoring_source("pit-demo"), START + timedelta(seconds=60)
        )
        assert expired.summary.loadPercent is None


async def test_latest_profile_has_changeable_floor_reference_without_invented_height(
    client,
    test_db_session,
    compact_config,
    monkeypatch,
):
    assert (await send(client, scan_message(0), compact_config)).status_code == 201
    assert (
        await client.get("/api/v2/monitoring/profiles", params={"pit_id": "pit-demo"})
    ).status_code == 401
    await login(client)
    response = await client.get("/api/v2/monitoring/profiles", params={"pit_id": "pit-demo"})
    assert response.status_code == 200
    profiles = response.json()["profiles"]
    assert len(profiles) == 2
    assert all(p["referenceDistanceCm"] == 1000 for p in profiles)
    assert all(p["average"] is None and p["samples"] == [] for p in profiles)
    monkeypatch.setenv("EDGE_V2_EMPTY_FLOOR_DISTANCE_CM", "1200")
    get_settings.cache_clear()
    changed = await lidar_profiles(test_db_session, monitoring_source("pit-demo"))
    assert all(p.referenceDistanceCm == 1200 for p in changed.profiles)


async def test_status_ordering_and_instance_restart_are_preserved(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config, START, sequence=5)
    older = status("lower-sequence", START + timedelta(seconds=2), 4)
    assert (await send(client, older, compact_config)).status_code == 201
    result = await device_status(
        test_db_session, "site-demo", "edge-demo", START + timedelta(seconds=2)
    )
    assert result.snapshot.sequence == 5
    restart = status("new-instance", START + timedelta(seconds=3), 1)
    restart["orchestrator_instance_id"] = "new-synthetic-instance"
    assert (await send(client, restart, compact_config)).status_code == 201
    result = await device_status(
        test_db_session, "site-demo", "edge-demo", START + timedelta(seconds=3)
    )
    assert result.snapshot.orchestrator_instance_id == "new-synthetic-instance"


async def test_baseline_upgrade_preserves_existing_large_payload():
    # Use an independent schema/DB: the fixture already contains current-model tables.
    # Preservation itself is also exercised by the legacy adoption and transfer suites.
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as db:

            def baseline(conn):
                config = migration_config()
                config.attributes["connection"] = conn
                command.upgrade(config, "0001_initial")

            await db.run_sync(baseline)
            body = json.dumps({"synthetic-points": list(range(1000))})
            await db.execute(
                text(
                    "INSERT INTO edge_v2_messages (site_id,edge_id,message_id,message_type,"
                    "config_revision,generated_at,received_at,body_hash,payload,created_at) "
                    "VALUES ('s','e','m','lidar_measurement','c',:t,:t,:h,:p,:t)"
                ),
                {"t": START.isoformat(), "h": "a" * 64, "p": body},
            )
            await db.run_sync(upgrade_schema)
            await db.run_sync(require_current_schema)
            assert (
                await db.execute(text("SELECT payload,compact FROM edge_v2_messages"))
            ).one() == (body, 0)
    finally:
        await engine.dispose()


async def test_worker_delay_does_not_delay_an_already_sustained_threshold(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(8):
        await sample(client, test_db_session, compact_config, second, 95)
    await process_source(
        test_db_session, monitoring_source("pit-demo"), START + timedelta(seconds=7)
    )
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 1
    await process_source(
        test_db_session, monitoring_source("pit-demo"), START + timedelta(seconds=20)
    )
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 1


async def test_input_gap_and_method_change_require_a_new_filter_window(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(5):
        await sample(client, test_db_session, compact_config, second, 95, True)
    result = await sample(client, test_db_session, compact_config, 8, 95, True)
    assert result.summary.filterState == "WARMING"
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 0
    for second in range(9, 13):
        await sample(client, test_db_session, compact_config, second, 95)
    changed = assessment("new-method", 0.95, START + timedelta(seconds=13))
    changed["fill_estimate"]["method_version"] = "synthetic-method-2"
    assert (await send(client, changed, compact_config)).status_code == 201
    result = await build_snapshot(
        test_db_session, monitoring_source("pit-demo"), START + timedelta(seconds=13)
    )
    assert result.summary.loadPercent is None
    assert result.summary.filterState == "WARMING"


async def test_status_alias_with_same_sequence_keeps_valid_snapshot(
    client,
    test_db_session,
    compact_config,
):
    original = status("original-status", START, 5)
    assert (await send(client, original, compact_config)).status_code == 201
    alias = {**original, "message_id": "alias-status"}
    assert (
        await send(client, alias, compact_config, START + timedelta(seconds=1))
    ).status_code == 201
    result = await device_status(
        test_db_session, "site-demo", "edge-demo", START + timedelta(seconds=1)
    )
    assert result.snapshot is not None and result.snapshot.sequence == 5
    assert result.connection_state == "REPORTING"


async def test_event_revisions_and_error_transitions_survive_periodic_compaction(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for revision, stage in [(2, "RETRACTED"), (1, "CONFIRMED")]:
        event = assessment(f"event-{revision}", 0.7, START)
        event.update(
            assessment_type="EVENT",
            assessment_id="collection-synthetic",
            revision=revision,
            event={"kind": "COLLECTION", "stage": stage},
        )
        assert (await send(client, event, compact_config)).status_code == 201
    for second in range(5):
        await sample(client, test_db_session, compact_config, second, 70)
    bad = status("bad-device", START + timedelta(seconds=5), 2)
    bad["overall_state"] = "MEASUREMENT_UNAVAILABLE"
    assert (await send(client, bad, compact_config)).status_code == 201
    await healthy(client, compact_config, START + timedelta(seconds=6), "healthy-again", 3)
    source = monitoring_source("pit-demo")
    events = await collection_events(test_db_session, source, 20, 0)
    assert events.totalItems == 1 and events.items[0].revision == 2
    assert events.items[0].event.stage == "RETRACTED"
    transitions = await state_events(test_db_session, source, 20, 0)
    assert transitions.totalItems == 2
    assert {e.detail["current"] for e in transitions.items} == {
        "HEALTHY",
        "MEASUREMENT_UNAVAILABLE",
    }
    await login(client)
    response = await client.get(
        "/api/v2/monitoring/state-events", params={"pit_id": "pit-demo", "limit": 1}
    )
    assert response.status_code == 200 and response.json()["totalItems"] == 2
    assert len(response.json()["items"]) == 1


async def test_independent_writers_serialize_first_projection_and_replay(
    test_engine, compact_config, tmp_path
):
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.app.schemas.edge_v2 import Measurement
    from src.app.services.edge_v2_service import accept_message

    engine = test_engine
    if test_engine.dialect.name == "sqlite":
        # Independent writers need distinct real connections, not one shared :memory: connection.
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(upgrade_schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    payload = scan_message(0)
    parsed = Measurement.model_validate(payload)
    device = get_settings().EDGE_V2_DEVICES[0]

    async def write():
        async with factory() as db:
            return await accept_message(db, parsed, payload, device)

    try:
        acknowledgments = await asyncio.gather(write(), write(), write())
        assert sum(not ack.duplicate for ack in acknowledgments) == 1
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(EdgeV2Latest)) == 1
            assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 1
    finally:
        if engine is not test_engine:
            await engine.dispose()


async def test_subminute_history_does_not_show_a_future_report(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    for second in range(10):
        await sample(client, test_db_session, compact_config, second, 70 + second)
    history = await load_history(
        test_db_session,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(seconds=7),
        60,
        START + timedelta(seconds=10),
    )
    point = history.samples[0]
    assert point.valuePercent == 74
    assert point.generatedAt is None and point.assessmentId is None
    assert point.reasonCodes == ["COMPACT_SUBMINUTE_METADATA_NOT_RETAINED"]


async def test_a_different_device_cannot_inherit_the_previous_devices_critical_state(
    client,
    test_db_session,
    compact_config,
):
    await healthy(client, compact_config)
    test_db_session.add(
        EdgeV2Operation(
            pit_id="pit-demo",
            site_id="different-site",
            edge_id="different-edge",
            state="CRITICAL",
            message_id="old-device-message",
            generated_at=START,
            observed_at=START,
            last_alert_at=None,
            last_alert_id=None,
        )
    )
    await test_db_session.commit()
    for second in range(5):
        await sample(client, test_db_session, compact_config, second, 88, True)
    row = await test_db_session.get(EdgeV2Operation, "pit-demo")
    assert row.state == "WARNING" and row.edge_id == "edge-demo"
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 0
