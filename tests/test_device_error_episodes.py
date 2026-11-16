"""Repeated legacy device state changes retain evidence without duplicate public events."""

from sqlalchemy import select

from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.services.error_history_service import error_history_events
from tests.test_monitoring_v2 import NOW, monitoring_config, send  # noqa: F401
from tests.test_scan_update_alarms import compact as compact
from tests.test_scan_update_alarms import report
from tests.test_v2_event_clips import camera as camera


def legacy(index, level, reasons, *, instance="orch-demo"):
    value = report(index, instance=instance)
    for sensor in value["sensors"]:
        sensor.pop("scan_update_alarm")
    value["overall_state"] = level
    value["reason_codes"] = reasons
    if "OUTBOX_DATA_LOSS" in reasons:
        value["storage"]["lost_messages"] = 5
    return value


async def test_repeated_causes_one_public_event_until_healthy(client, test_db_session):
    db = test_db_session
    values = [
        ("HEALTHY", []),
        ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
        ("MEASUREMENT_UNAVAILABLE", ["OUTBOX_DATA_LOSS", "SCAN_NOT_UPDATED"]),
        ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
        ("MEASUREMENT_UNAVAILABLE", ["SCAN_NOT_UPDATED", "OUTBOX_DATA_LOSS"]),
        ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
        ("HEALTHY", []),
        ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
    ]
    for index, (level, reasons) in enumerate(values):
        await send(client, legacy(index, level, reasons))
    raw = list(
        await db.scalars(
            select(EdgeV2Transition)
            .where(EdgeV2Transition.kind == "DEVICE_STATE_CHANGED")
            .order_by(EdgeV2Transition.id)
        )
    )
    assert len(raw) == 7
    assert [r.payload.get("deviceEpisodeContinuation", False) for r in raw] == [
        False,
        False,
        True,
        True,
        True,
        False,
        False,
    ]
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert len(events) == 4
    assert len({event.id for event in events}) == 4
    assert sum(event.stateAtOccurrence == "RECOVERED" for event in events) == 1
    from src.app.services.event_recording_queries import event_table
    from src.app.services.monitoring_v2_service import monitoring_source

    table = event_table(
        db, "pit-demo", monitoring_source("pit-demo"), "error", None, None, NOW, 30, 30, None
    )
    assert set(await db.scalars(select(table.c.event_id))) == {event.id for event in events}
    page = await client.get("/api/v1/events?pit_id=pit-demo&category=error")
    assert page.status_code == 200 and page.json()["page"]["totalItems"] == 4
    pointer = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "device_error_episodes"))
    assert pointer.payload["causes"] == ["reason:OUTBOX_DATA_LOSS"]


async def test_new_cause_scope_change_and_replay_remain_distinct(client, test_db_session):
    for index, (level, reasons, instance) in enumerate(
        [
            ("HEALTHY", [], "one"),
            ("DEGRADED", ["OUTBOX_DATA_LOSS"], "one"),
            ("CONFIG_ERROR", ["OTHER_FAULT"], "one"),
            ("DEGRADED", ["OUTBOX_DATA_LOSS"], "two"),
        ]
    ):
        payload = legacy(index, level, reasons, instance=instance)
        await send(client, payload)
        duplicate = await send(client, payload)
        assert duplicate.json()["duplicate"] is True
    events = await error_history_events(test_db_session, "pit-demo", None, None, NOW)
    assert len(events) == 3
    assert any(event.title == "장치 설정 오류" for event in events)


async def test_repeated_episode_does_not_enqueue_another_error_clip(
    client, test_db_session, camera
):
    from src.app.models.media_job import MediaClipJob
    from src.app.services.v2_event_clip_service import queue_recent_events

    db = test_db_session
    from datetime import timedelta

    await queue_recent_events(db, "synthetic-session", NOW - timedelta(seconds=25))
    for index, (level, reasons) in enumerate(
        [
            ("HEALTHY", []),
            ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
            ("MEASUREMENT_UNAVAILABLE", ["OUTBOX_DATA_LOSS", "SCAN_NOT_UPDATED"]),
            ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
            ("MEASUREMENT_UNAVAILABLE", ["OUTBOX_DATA_LOSS", "SCAN_NOT_UPDATED"]),
        ]
    ):
        await send(client, legacy(index, level, reasons))
    await queue_recent_events(db, "synthetic-session", NOW)
    jobs = list(await db.scalars(select(MediaClipJob)))
    assert len(jobs) == 1 and jobs[0].category == "v2_error"
    await queue_recent_events(db, "synthetic-session", NOW)
    assert len(list(await db.scalars(select(MediaClipJob)))) == 1


async def test_first_visible_change_after_unhealthy_baseline_is_not_hidden(client, test_db_session):
    await send(client, legacy(0, "DEGRADED", ["OUTBOX_DATA_LOSS"]))
    await send(client, legacy(1, "MEASUREMENT_UNAVAILABLE", ["OUTBOX_DATA_LOSS"]))
    events = await error_history_events(test_db_session, "pit-demo", None, None, NOW)
    assert len(events) == 1 and events[0].stateAtOccurrence == "UNAVAILABLE"


async def test_unknown_report_does_not_confirm_recovery(client, test_db_session):
    for index, (level, reasons) in enumerate(
        [
            ("HEALTHY", []),
            ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
            ("UNKNOWN", []),
            ("DEGRADED", ["OUTBOX_DATA_LOSS"]),
        ]
    ):
        await send(client, legacy(index, level, reasons))
    events = await error_history_events(test_db_session, "pit-demo", None, None, NOW)
    assert len(events) == 2
    assert not any(e.stateAtOccurrence == "RECOVERED" for e in events)


async def test_revision_change_does_not_reuse_old_suppression(client, test_db_session):
    for index, (level, revision) in enumerate(
        [
            ("HEALTHY", "cfg-demo"),
            ("DEGRADED", "cfg-demo"),
            ("MEASUREMENT_UNAVAILABLE", "cfg-next"),
        ]
    ):
        value = legacy(index, level, [] if level == "HEALTHY" else ["OUTBOX_DATA_LOSS"])
        value["config_revision"] = revision
        await send(client, value)
    events = await error_history_events(test_db_session, "pit-demo", None, None, NOW)
    assert len(events) == 2
