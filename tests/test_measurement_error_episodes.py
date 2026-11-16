"""Recovery needs live continuous evidence, while raw validity and current health stay real."""

import csv
import io
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.fill_observation import FillObservation
from src.app.services.error_history_service import error_transition_event
from src.app.services.measurement_error_episodes import mark_measurement_episode
from tests.test_compact_lidar_storage import START, healthy, send
from tests.test_compact_lidar_storage import compact_config as compact_config
from tests.test_monitoring_v2 import assessment, login
from tests.test_v2_event_clips import camera as camera

KEY = ("site-demo", "edge-demo", "measurement_error_episodes")


async def rows(db):
    return list(
        await db.scalars(
            select(EdgeV2Transition)
            .where(EdgeV2Transition.kind == "MEASUREMENT_VALIDITY_CHANGED")
            .order_by(EdgeV2Transition.id)
        )
    )


async def visible(db):
    return [e for row in await rows(db) if (e := error_transition_event(row)) is not None]


async def put(client, clock, second, valid=True, reasons=None):
    value = assessment(f"episode-{second}", 0.5, START + timedelta(seconds=second))
    if not valid:
        value["decision_state"] = "UNAVAILABLE"
        value["fill_estimate"] = None
    value["reason_codes"] = reasons or ([] if valid else ["HEIGHT_SCAN_STALE"])
    response = await send(client, value, clock)
    assert response.status_code == 201, response.text
    return value


async def test_flapping_preserves_raw_changes_and_confirms_one_recovery(
    client, test_db_session, compact_config
):
    db, clock = test_db_session, compact_config
    await healthy(client, clock)
    await put(client, clock, 1)
    await put(client, clock, 2, False)
    await put(client, clock, 3)
    await put(client, clock, 4, False)
    await put(client, clock, 5)
    # Public recovery is pending, while actual valid input is already used.
    from src.app.models.edge_v2_projection import EdgeV2Filter

    assert (await db.get(EdgeV2Filter, ("site-demo", "edge-demo"))).payload["input_valid"] is True
    for second in range(6, 15):
        await put(client, clock, second)
    assert [e.stateAtOccurrence for e in await visible(db)] == ["UNAVAILABLE"]
    await put(client, clock, 15)
    assert [e.stateAtOccurrence for e in await visible(db)] == ["UNAVAILABLE", "RECOVERED"]
    raw = await rows(db)
    assert len(raw) == 5
    assert [(r.payload["previousValid"], r.payload["currentValid"]) for r in raw] == [
        (True, False),
        (False, True),
        (True, False),
        (False, True),
        (True, True),
    ]
    assert raw[-1].payload["recoveryConfirmSeconds"] == 10
    assert all(r.payload["messageId"] and r.payload["observationId"] for r in raw)
    await put(client, clock, 16, False)
    assert [e.stateAtOccurrence for e in await visible(db)] == [
        "UNAVAILABLE",
        "RECOVERED",
        "UNAVAILABLE",
    ]


async def test_new_reason_during_invalid_input_is_visible_without_fake_boolean_change(
    client, test_db_session, compact_config
):
    db, clock = test_db_session, compact_config
    await healthy(client, clock)
    await put(client, clock, 1)
    await put(client, clock, 2, False, ["HEIGHT_SCAN_STALE"])
    await put(client, clock, 3, False, ["HEIGHT_SCAN_STALE", "CLOCK_SKEW"])
    duplicate = await put(client, clock, 4, False, ["CLOCK_SKEW", "HEIGHT_SCAN_STALE"])
    response = await send(client, duplicate, clock)
    assert response.json()["duplicate"] is True
    events = await visible(db)
    assert len(events) == 2
    raw = await rows(db)
    assert raw[1].payload["previousValid"] is False
    assert raw[1].payload["currentValid"] is False
    assert events[1].reasonCodes == ["HEIGHT_SCAN_STALE", "CLOCK_SKEW"]


async def test_pages_csv_and_error_clip_queue_agree(
    client, test_db_session, compact_config, camera, monkeypatch
):
    from src.app.models.media_job import MediaClipJob
    from src.app.services import error_history_service, history_csv
    from src.app.services.v2_event_clip_service import queue_recent_events

    db, clock = test_db_session, compact_config
    await queue_recent_events(db, "synthetic-session", START)
    await healthy(client, clock)
    for second, valid, reasons in [
        (1, True, []),
        (2, False, ["HEIGHT_SCAN_STALE"]),
        (3, True, []),
        (4, False, ["HEIGHT_SCAN_STALE"]),
        (5, False, ["CLOCK_SKEW"]),
    ]:
        await put(client, clock, second, valid, reasons)
    for second in range(6, 17):
        await put(client, clock, second)
    expected = {e.id for e in await visible(db)}
    assert len(expected) == 3
    camera["clock"]["now"] = START + timedelta(seconds=17)
    monkeypatch.setattr(history_csv, "datetime", error_history_service.datetime)
    await login(client)
    response = await client.get("/api/v1/events?pit_id=pit-demo&category=error&page_size=1")
    assert response.status_code == 200, response.text
    assert response.json()["page"]["totalItems"] == 3
    exported = await client.get(
        "/api/v1/events.csv",
        params={
            "pit_id": "pit-demo",
            "category": "error",
            "from": START.isoformat(),
            "to": (START + timedelta(seconds=17)).isoformat(),
        },
    )
    assert exported.status_code == 200, exported.text
    records = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert {r["id"] for r in records} == expected
    await queue_recent_events(db, "synthetic-session", START + timedelta(seconds=17))
    jobs = list(await db.scalars(select(MediaClipJob)))
    assert len(jobs) == 2
    await queue_recent_events(db, "synthetic-session", START + timedelta(seconds=17))
    assert len(list(await db.scalars(select(MediaClipJob)))) == 2


async def feed(
    db, second, valid, previous, *, observed=None, received=None, scope="one", reasons=None
):
    observation = FillObservation(
        source="EDGE_STATE",
        site_id="site-demo",
        edge_id="edge-demo",
        config_revision="cfg-demo",
        message_id=f"input-{second}",
        observation_id=f"input-{second}",
        generated_at=START + timedelta(seconds=second),
        received_at=START + timedelta(seconds=second if received is None else received),
        observed_to=START + timedelta(seconds=second if observed is None else observed),
        rule_version="rule-1",
        method_version="method-1",
        value_percent=50 if valid else None,
        data_state="AVAILABLE" if valid else "UNAVAILABLE",
        reason_codes=reasons or ([] if valid else ["HEIGHT_SCAN_STALE"]),
    )
    transition = None
    if previous is not None and previous != valid:
        transition = EdgeV2Transition(
            site_id=observation.site_id,
            edge_id=observation.edge_id,
            occurred_at=observation.generated_at,
            kind="MEASUREMENT_VALIDITY_CHANGED",
            payload={"previousValid": previous, "currentValid": valid},
        )
        db.add(transition)
    await mark_measurement_episode(
        db, observation, valid, [scope], previous, "pit-demo", transition
    )
    await db.flush()


@pytest.mark.parametrize("mode", ["gap", "burst", "repeated", "future"])
async def test_missing_or_untrusted_observations_cannot_confirm_recovery(test_db_session, mode):
    db = test_db_session
    await feed(db, 0, True, None)
    await feed(db, 1, False, True)
    for second in range(2, 13):
        options = {}
        if mode == "gap":
            if second == 7:
                continue
        elif mode == "burst":
            options["received"] = 30 + second / 100
        elif mode == "repeated":
            options["observed"] = 2
        else:
            options["observed"] = second + 1
        await feed(db, second, True, second != 2, **options)
    assert [e.stateAtOccurrence for e in await visible(db)] == ["UNAVAILABLE"]


async def test_restart_retains_episode_but_scope_change_does_not_close_old_error(test_db_session):
    db = test_db_session
    await feed(db, 0, True, None)
    await feed(db, 1, False, True)
    for second in range(2, 8):
        await feed(db, second, True, second != 2)
    await db.commit()
    db.expunge_all()
    for second in range(8, 13):
        await feed(db, second, True, True)
    assert [e.stateAtOccurrence for e in await visible(db)] == ["UNAVAILABLE", "RECOVERED"]
    await feed(db, 13, False, True)
    await feed(db, 14, False, False, scope="two")
    assert len(await visible(db)) == 4
    for second in range(15, 20):
        await feed(db, second, True, second != 15, scope="three")
    assert len(await visible(db)) == 4


async def test_startup_does_not_invent_error_and_normal_input_does_not_rewrite_state(
    test_db_session,
):
    db = test_db_session
    await feed(db, 0, False, None)
    await feed(db, 1, True, False)
    assert not await visible(db)
    row = await db.get(EdgeV2Latest, KEY)
    first_message = row.message_id
    await feed(db, 2, True, True)
    assert row.message_id == first_message
    await feed(db, 3, False, True)
    assert len(await visible(db)) == 1


async def test_episode_and_raw_transition_rollback_together(test_db_session):
    db = test_db_session
    await feed(db, 0, True, None)
    await db.commit()
    await feed(db, 1, False, True)
    assert len(await visible(db)) == 1
    await db.rollback()
    assert not await rows(db)
    assert (await db.get(EdgeV2Latest, KEY)).payload["causes"] == []
    await feed(db, 2, False, True)
    assert len(await visible(db)) == 1


async def test_reason_bound_overflow_and_corrupt_state_do_not_hide_new_error(test_db_session):
    db = test_db_session
    await feed(db, 0, True, None)
    for second in range(1, 131):
        await feed(db, second, False, second == 1, reasons=[f"FAULT_{second}"])
    assert len(await visible(db)) == 130
    row = await db.get(EdgeV2Latest, KEY)
    assert len(row.payload["causes"]) <= 128
    row.payload = {"version": 1, "scope": "broken", "baseline": "invalid"}
    await db.flush()
    await feed(db, 131, False, False)
    assert len(await visible(db)) == 131


async def test_provenance_reason_does_not_restart_same_error(test_db_session):
    db = test_db_session
    await feed(db, 0, True, None)
    await feed(db, 1, False, True)
    await feed(db, 2, True, False)
    await feed(db, 3, False, True, reasons=["HEIGHT_SCAN_STALE", "SIMULATION_GEOMETRY"])
    assert len(await visible(db)) == 1
    assert len(await rows(db)) == 3
