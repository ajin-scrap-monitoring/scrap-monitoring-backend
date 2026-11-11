"""Invented status reports: confirmed scan alarms never relax calculation freshness."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.edge_v2 import ScanUpdateAlarm
from src.app.services.active_health_service import health_conditions
from src.app.services.error_history_service import error_history_events
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from tests.test_monitoring_v2 import NOW, TOKEN, send, status
from tests.test_monitoring_v2 import monitoring_config as monitoring_config


@pytest.fixture(autouse=True)
def compact(monkeypatch):
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def alarm(state="CLEAR", age=0, at=NOW):
    reasons = {
        "UNKNOWN": [],
        "CLEAR": [],
        "PENDING": ["SCAN_UPDATE_DELAY_PENDING"],
        "ACTIVE": ["SCAN_UPDATE_DELAY_CONFIRMED"],
        "RECOVERING": ["SCAN_UPDATE_RECOVERING"],
    }
    return {
        "state": state,
        "active": None if state == "UNKNOWN" else state in ("ACTIVE", "RECOVERING"),
        "scan_age_ms": age,
        "last_valid_scan_at": at.isoformat() if at else None,
        "confirm_after_ms": 3000,
        "reason_codes": reasons[state],
    }


def report(index, states=("CLEAR", "CLEAR"), *, loss=False, instance="orch-demo"):
    when = NOW - timedelta(seconds=20 - index)
    result = status(f"scan-alarm-{index}", when, index + 1)
    result["orchestrator_instance_id"] = instance
    for sensor, state in zip(result["sensors"], states, strict=True):
        age = {"CLEAR": 0, "PENDING": 1800, "ACTIVE": 3001, "RECOVERING": 100, "UNKNOWN": 0}[state]
        at = when - timedelta(milliseconds=age)
        sensor.update(last_scan_at=at.isoformat(), scan_update_alarm=alarm(state, age, at))
        sensor["data_state"] = "STALE" if state in ("PENDING", "ACTIVE") else "FRESH"
        sensor["reason_codes"] = ["SCAN_NOT_UPDATED"] if sensor["data_state"] == "STALE" else []
    if all(s["data_state"] == "STALE" for s in result["sensors"]):
        result["overall_state"] = "MEASUREMENT_UNAVAILABLE"
        result["reason_codes"] = ["SCAN_NOT_UPDATED"]
    elif any(s["data_state"] != "FRESH" for s in result["sensors"]):
        result["overall_state"] = "DEGRADED"
        result["reason_codes"] = ["SCAN_NOT_UPDATED"]
    if loss:
        result["reason_codes"].append("OUTBOX_DATA_LOSS")
        result["storage"]["lost_messages"] = 5
        if result["overall_state"] == "HEALTHY":
            result["overall_state"] = "DEGRADED"
    return result


@pytest.mark.parametrize("state", ["UNKNOWN", "CLEAR", "PENDING", "ACTIVE", "RECOVERING"])
def test_operational_alarm_round_trip_and_exact_state_contract(state):
    value = alarm(state)
    parsed = ScanUpdateAlarm.model_validate(value)
    assert parsed.model_dump(mode="json")["state"] == state
    for field, bad in [
        ("active", not value["active"]),
        ("reason_codes", ["OTHER"]),
        ("confirm_after_ms", 3000.0),
        ("scan_age_ms", -1),
        ("extra", True),
    ]:
        with pytest.raises(ValidationError):
            ScanUpdateAlarm.model_validate({**value, field: bad})


async def test_receiver_keeps_optional_field_rejects_bad_combo_and_preserves_duplicates(
    client, test_db_session
):
    value = report(0)
    response = await send(client, value)
    assert response.json()["duplicate"] is False
    assert (await send(client, value)).json()["duplicate"] is True
    row = await test_db_session.get(EdgeV2Latest, ("site-demo", "edge-demo", "status"))
    assert (
        row.payload["sensors"][0]["scan_update_alarm"] == value["sensors"][0]["scan_update_alarm"]
    )
    bad = report(1)
    bad["sensors"][0]["scan_update_alarm"]["active"] = True
    response = await client.post(
        "/api/edge/v2/status",
        json=bad,
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": bad["message_id"]},
    )
    assert response.status_code == 422
    snapshot = await build_snapshot(test_db_session, monitoring_source("pit-demo"), NOW)
    assert snapshot.deviceStatus.snapshot.sensors[0].scan_update_alarm.state == "CLEAR"


@pytest.mark.parametrize("loss", [False, True])
async def test_pending_clear_keeps_raw_diagnostics_but_no_repeated_visible_error(
    client, test_db_session, loss
):
    db = test_db_session
    for i, state in enumerate(["CLEAR", "PENDING", "CLEAR", "PENDING", "CLEAR"]):
        value = report(i, (state, state), loss=loss)
        await send(client, value)
        snapshot = await build_snapshot(
            db, monitoring_source("pit-demo"), NOW - timedelta(seconds=20 - i)
        )
        items = health_conditions(snapshot, NOW - timedelta(seconds=20 - i))[0]
        assert not any(x.componentKind == "LIDAR" for x in items)
        edges = [x for x in items if x.componentKind == "EDGE"]
        assert bool(edges) == loss
        if loss:
            assert edges[0].reasonCodes == ["OUTBOX_DATA_LOSS"]
        if state == "PENDING":
            assert snapshot.deviceStatus.snapshot.overall_state == "MEASUREMENT_UNAVAILABLE"
            assert snapshot.deviceStatus.snapshot.sensors[0].data_state == "STALE"
    rows = list(await db.scalars(select(EdgeV2Transition)))
    assert len(rows) == 4 and all(x.payload["diagnosticOnly"] for x in rows)
    assert await error_history_events(db, "pit-demo", None, None, NOW) == []
    page = await client.get("/api/v1/events?pit_id=pit-demo&category=error")
    assert page.status_code == 200 and page.json()["page"]["totalItems"] == 0


async def test_active_recovering_clear_is_one_episode_per_sensor_and_never_read_driven(
    client, test_db_session
):
    db = test_db_session
    for i, state in enumerate(["CLEAR", "PENDING", "ACTIVE", "RECOVERING", "CLEAR"]):
        value = report(i, (state, "CLEAR"))
        await send(client, value)
        snapshot = await build_snapshot(
            db, monitoring_source("pit-demo"), NOW - timedelta(seconds=20 - i)
        )
        items = health_conditions(snapshot, NOW - timedelta(seconds=20 - i))[0]
        delayed = [x for x in items if x.componentKind == "LIDAR"]
        assert len(delayed) == (1 if state in ("ACTIVE", "RECOVERING") else 0)
        if delayed:
            assert delayed[0].componentId == "lidar-a"
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert [e.title for e in events] == ["LiDAR 스캔 갱신 복구", "LiDAR 스캔 갱신 지연"]
    assert [e.stateAtOccurrence for e in events] == ["RECOVERED", "UNAVAILABLE"]
    assert len(await error_history_events(db, "pit-demo", None, None, NOW)) == 2


@pytest.mark.parametrize("missing", [True, False])
async def test_missing_unknown_never_resolves_episode_or_hides_current_unknown(
    client, test_db_session, missing
):
    db = test_db_session
    await send(client, report(0, ("ACTIVE", "CLEAR")))
    value = report(1, ("PENDING", "CLEAR"))
    if missing:
        value["sensors"][0].pop("scan_update_alarm")
    else:
        value["sensors"][0]["scan_update_alarm"] = alarm("UNKNOWN", None, None)
    await send(client, value)
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert not any(e.title == "LiDAR 스캔 갱신 복구" for e in events)
    row = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "scan_update_alarms"))
    assert row.payload["entries"]["lidar-a"] is True
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), NOW - timedelta(seconds=19))
    assert any(
        x.componentKind == "LIDAR"
        for x in health_conditions(snapshot, NOW - timedelta(seconds=19))[0]
    )


async def test_legacy_sender_fault_other_reason_and_restart_are_not_silenced(
    client, test_db_session
):
    db = test_db_session
    legacy = report(0, ("PENDING", "PENDING"))
    for sensor in legacy["sensors"]:
        sensor.pop("scan_update_alarm")
    await send(client, legacy)
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), NOW - timedelta(seconds=20))
    assert any(
        x.componentKind == "EDGE"
        for x in health_conditions(snapshot, NOW - timedelta(seconds=20))[0]
    )
    explicit = report(1, ("PENDING", "PENDING"))
    explicit["overall_state"] = "CONFIG_ERROR"
    explicit["reason_codes"] = ["SYNTHETIC_CONFIGURATION_ERROR"]
    explicit["sensors"][0]["reason_codes"].append("SYNTHETIC_SENSOR_ERROR")
    await send(client, explicit)
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), NOW - timedelta(seconds=19))
    assert any(
        x.componentKind == "EDGE" and x.state == "CONFIG_ERROR"
        for x in health_conditions(snapshot, NOW - timedelta(seconds=19))[0]
    )
    await send(client, report(2, ("ACTIVE", "CLEAR")))
    await send(client, report(3, instance="new-synthetic-instance"))
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert not any(e.title == "LiDAR 스캔 갱신 복구" for e in events)


@pytest.mark.parametrize("case", ["missing", "unknown", "pending"])
async def test_confirmed_episode_stays_unresolved_until_explicit_clear(
    client, test_db_session, case
):
    from src.app.services.active_alerts_v2_service import active_fill_alerts

    db = test_db_session
    await send(client, report(0, ("ACTIVE", "CLEAR")))
    value = report(1)
    if case == "missing":
        value["sensors"][0].pop("scan_update_alarm")
    elif case == "unknown":
        value["sensors"][0]["scan_update_alarm"] = alarm("UNKNOWN", None, None)
    else:
        value = report(1, ("PENDING", "CLEAR"))
    await send(client, value)
    result = await active_fill_alerts(db, monitoring_source("pit-demo"), NOW, include_health=True)
    assert any(
        x.componentKind == "LIDAR"
        and x.componentId == "lidar-a"
        and "SCAN_UPDATE_ALARM_UNCONFIRMED" in x.reasonCodes
        for x in result.items
        if hasattr(x, "componentKind")
    )
    assert not any(
        e.title == "LiDAR 스캔 갱신 복구"
        for e in await error_history_events(db, "pit-demo", None, None, NOW)
    )
    await send(client, report(2))
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert sum(e.title == "LiDAR 스캔 갱신 복구" for e in events) == 1


async def test_out_of_order_and_duplicate_status_cannot_create_or_clear_scan_alarm(
    client, test_db_session
):
    db = test_db_session
    active = report(3, ("ACTIVE", "CLEAR"))
    await send(client, active)
    await send(client, active)
    await send(client, report(1))
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert [e.title for e in events] == ["LiDAR 스캔 갱신 지연"]
    for i, states in [
        (4, ("ACTIVE", "ACTIVE")),
        (5, ("CLEAR", "RECOVERING")),
        (6, ("CLEAR", "CLEAR")),
    ]:
        await send(client, report(i, states))
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert sum(e.title == "LiDAR 스캔 갱신 지연" for e in events) == 2
    assert sum(e.title == "LiDAR 스캔 갱신 복구" for e in events) == 2


async def test_non_scan_reason_change_remains_visible_when_operational_severity_is_same(
    client, test_db_session
):
    db = test_db_session
    first = report(0)
    first["services"] = [
        {
            "service": "synthetic-driver",
            "state": "UNKNOWN",
            "last_progress_at": None,
            "reason_codes": ["STATUS_UNAVAILABLE"],
        }
    ]
    first["overall_state"] = "DEGRADED"
    await send(client, first)
    second = report(1, ("PENDING", "PENDING"), loss=True)
    second["services"] = first["services"]
    await send(client, second)
    events = await error_history_events(db, "pit-demo", None, None, NOW)
    assert len(events) == 1 and events[0].reasonCodes == ["OUTBOX_DATA_LOSS"]


async def test_unresolved_alarm_never_downgrades_explicit_sensor_unavailable(
    client, test_db_session
):
    from src.app.services.active_alerts_v2_service import active_fill_alerts

    db = test_db_session
    await send(client, report(0, ("ACTIVE", "CLEAR")))
    value = report(1)
    value["sensors"][0].update(data_state="UNAVAILABLE", last_scan_at=None)
    value["sensors"][0]["scan_update_alarm"] = alarm("UNKNOWN", None, None)
    await send(client, value)
    result = await active_fill_alerts(db, monitoring_source("pit-demo"), NOW, include_health=True)
    assert any(
        x.componentKind == "LIDAR"
        and x.componentId == "lidar-a"
        and x.state == "UNAVAILABLE"
        and x.severity == "error"
        for x in result.items
        if hasattr(x, "componentKind")
    )
