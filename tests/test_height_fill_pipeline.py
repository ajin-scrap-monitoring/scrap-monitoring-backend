"""Synthetic end-to-end observations: height -> temporal median -> history -> collection signal."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import Settings, get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest, EdgeV2Minute
from src.app.models.log import AlertLog
from src.app.services.edge_v2_operations import process_source
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from tests.test_edge_v2 import assessment as unavailable_state
from tests.test_lidar_height import REFERENCE, inputs
from tests.test_monitoring_v2 import DEVICE, SOURCE, TOKEN, assessment, login, status

START = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def height_context(monkeypatch):
    clock = {"now": START}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", json.dumps([REFERENCE]))
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "true")
    monkeypatch.setenv("EDGE_V2_HEIGHT_FILL_ENABLED", "true")
    monkeypatch.setattr("src.app.services.edge_v2_service.datetime", Clock)
    monkeypatch.setattr("src.app.services.monitoring_v2_service.datetime", Clock)
    monkeypatch.setattr("src.app.services.load_history_v2_service.datetime", Clock)
    get_settings.cache_clear()
    yield clock
    get_settings.cache_clear()


async def send(client, payload):
    path = {
        "edge_configuration": "configurations",
        "edge_status": "status",
        "lidar_measurement": "measurements",
        "edge_assessment": "assessments",
    }[payload["message_type"]]
    return await client.post(
        "/api/edge/v2/" + path,
        json=payload,
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": payload["message_id"]},
    )


async def prepare(client, db, configured=True):
    db.add(
        PitConfig(
            id="pit-demo",
            name="Synthetic",
            total_depth_cm=200,
            warning_threshold_percent=75,
            critical_threshold_percent=90,
        )
    )
    await db.commit()
    c, _ = inputs()
    if configured:
        assert (await send(client, c)).status_code == 201
    s = status(when=START)
    s["config_revision"] = "cfg-1"
    assert (await send(client, s)).status_code == 201
    return c


async def frame(client, db, clock, second, percent, valid=True, operate=True, wire=True):
    now = START + timedelta(seconds=second)
    clock["now"] = now
    s = status(f"status-{second}", now, second + 2)
    s["config_revision"] = "cfg-1"
    assert (await send(client, s)).status_code == 201
    _, m = inputs()
    m.update(message_id=f"measurement-{second}", generated_at=now.isoformat())
    for index, sensor in enumerate(m["sensors"]):
        sensor["scan"].update(
            scan_id=f"scan-{index}-{second}",
            sequence=second,
            acquired_at=now.isoformat(),
            acquired_monotonic_ns=str((second + 1) * 1_000_000_000),
        )
        for point in sensor["scan"]["points"]:
            point["position_mm"]["z"] = round((percent / 100 * 200 - 50) * 10)
        if not valid:
            sensor.update(availability="NO_NEW_SCAN", scan=None)
    if not valid:
        m.update(pair_state="NONE", pair_delta_ms=None)
    response = await send(client, m)
    assert response.status_code == 201, response.text
    if wire:
        a = unavailable_state(f"unavailable-{second}")
        a["assessment_id"] = a["message_id"]
        a.update(
            generated_at=now.isoformat(), observed_from=now.isoformat(), observed_to=now.isoformat()
        )
        a["capabilities"]["fill"] = "NOT_IMPLEMENTED"
        a["reason_codes"] = ["FILL_METHOD_NOT_IMPLEMENTED"]
        a["evidence"]["scans"] = []
        assert (await send(client, a)).status_code == 201
    source = monitoring_source("pit-demo")
    if operate:
        await process_source(db, source, now)
    return m, await build_snapshot(db, source, now)


async def test_partial_height_history_keeps_the_selected_observation_metadata(
    client, test_db_session, height_context, monkeypatch
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = height_context["now"]
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.load_history_v2_service.datetime", Clock)
    db = test_db_session
    await prepare(client, db)
    selected_id = None
    for second in range(10):
        _, snapshot = await frame(client, db, height_context, second, 50 + second)
        if second == 6:
            selected_id = snapshot.fillObservation.observation_id
    await login(client)
    response = await client.get(
        "/api/v2/monitoring/load-history",
        params={
            "pit_id": "pit-demo",
            "from": START.isoformat(),
            "to": (START + timedelta(seconds=7)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    point = response.json()["samples"][0]
    assert point["valuePercent"] == 54 and point["valid"]
    assert point["inputSource"] == "HEIGHT_SECTIONS" and point["assessmentId"] is None
    assert point["observationId"] == selected_id
    assert datetime.fromisoformat(point["generatedAt"]) == START + timedelta(seconds=6)
    assert datetime.fromisoformat(point["observedAt"]) == START + timedelta(seconds=6)


async def test_height_spike_history_and_wire_provenance(client, test_db_session, height_context):
    db = test_db_session
    await prepare(client, db)
    for second in range(12):
        payload, snapshot = await frame(
            client, db, height_context, second, 99 if second == 7 else 50
        )
        assert snapshot.summary.loadPercent == (None if second < 4 else 50)
        assert snapshot.summary.inputSource == "HEIGHT_SECTIONS"
        assert snapshot.assessment.fill_estimate is None
        assert snapshot.assessment.capabilities.fill == "NOT_IMPLEMENTED"
    assert snapshot.fillObservation.source == "HEIGHT_SECTIONS"
    assert snapshot.fillObservation.message_id == payload["message_id"]
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=12), 60
    )
    sample = history.samples[0]
    assert (
        sample.valuePercent
        == sample.averagePercent
        == sample.minimumPercent
        == sample.maximumPercent
        == 50
    )
    assert sample.assessmentId is None and sample.observationId.startswith("height-")
    assert sample.inputSource == "HEIGHT_SECTIONS" and sample.methodVersion.startswith(
        "HEIGHT_SECTION_V2:"
    )
    assert (sample.validSamples, sample.invalidSamples) == (8, 4)
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0
    # Current alert starts add one bounded pointer per source, not one row per frame.
    assert await db.scalar(select(func.count()).select_from(EdgeV2Latest)) == 6
    active_starts = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "active_alert_starts"))
    assert active_starts is not None and active_starts.payload["entries"] == {}
    height_window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert "position_mm" not in json.dumps(height_window.payload)
    assert all(len(s["frames"]) <= 5 for s in height_window.payload["sensors"].values())
    minute = await db.scalar(select(EdgeV2Minute))
    assert "position_mm" not in json.dumps(minute.payload)
    assert len(minute.payload["values"]) <= 60


async def test_height_critical_confirmation_recovery_cooldown_and_alert_origin(
    client,
    test_db_session,
    height_context,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(7):
        _, snapshot = await frame(client, db, height_context, second, 95)
        assert snapshot.operations.lastKnownState != "CRITICAL"
    _, snapshot = await frame(client, db, height_context, 7, 95)
    assert snapshot.operations.effectiveState == "CRITICAL"
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 1
    for second in range(8, 11):
        _, snapshot = await frame(client, db, height_context, second, 88)
    assert snapshot.operations.effectiveState == "CRITICAL"
    for second in range(11, 14):
        _, snapshot = await frame(client, db, height_context, second, 85)
    assert snapshot.operations.effectiveState == "WARNING"
    for second in range(14, 22):
        _, snapshot = await frame(client, db, height_context, second, 95)
    assert snapshot.operations.effectiveState == "CRITICAL"
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 1
    await login(client)
    result = await client.get("/api/v2/monitoring/alerts?pit_id=pit-demo")
    assert result.status_code == 200
    alert = result.json()["items"][0]
    assert alert["assessmentId"] is None and alert["observationId"].startswith("height-")
    assert alert["inputSource"] == "HEIGHT_SECTIONS"
    assert "단면 높이 비율" in (await db.scalar(select(AlertLog))).message
    for second in range(1810, 1815):
        await frame(client, db, height_context, second, 95)
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 2


async def test_missing_input_duplicate_and_restart_do_not_extend_the_window(
    client,
    test_db_session,
    test_engine,
    height_context,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        payload, snapshot = await frame(client, db, height_context, second, 50, wire=False)
    assert snapshot.assessment is None and snapshot.summary.loadPercent == 50
    assert (await send(client, payload)).json()["duplicate"] is True
    changed = copy.deepcopy(payload)
    changed["sensors"][0]["scan"]["points"][0]["quality"] += 1
    assert (await send(client, changed)).status_code == 409
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as reopened:
        assert (
            await build_snapshot(reopened, monitoring_source("pit-demo"), height_context["now"])
        ).summary.loadPercent == 50
    _, snapshot = await frame(client, db, height_context, 5, 0, valid=False)
    assert snapshot.dataState == "UNAVAILABLE" and snapshot.summary.loadPercent is None
    for second in range(6, 10):
        _, snapshot = await frame(client, db, height_context, second, 0)
        assert snapshot.summary.loadPercent is None
    _, snapshot = await frame(client, db, height_context, 10, 0)
    assert snapshot.summary.loadPercent == 0
    assert len((await db.get(EdgeV2Filter, ("site-demo", "edge-demo"))).payload["samples"]) == 5


async def test_status_sensor_failure_invalidates_height_before_another_measurement(
    client,
    test_db_session,
    height_context,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    s = status("sensor-stale", START + timedelta(seconds=5), 100)
    s["config_revision"] = "cfg-1"
    s["sensors"][1]["data_state"] = "STALE"
    height_context["now"] = START + timedelta(seconds=5)
    assert (await send(client, s)).status_code == 201
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), height_context["now"])
    assert snapshot.dataState == "UNAVAILABLE" and snapshot.summary.loadPercent is None
    assert (await db.get(EdgeV2Filter, ("site-demo", "edge-demo"))).payload == {}


async def test_reference_change_invalidates_current_value_and_keeps_methods_separate(
    client,
    test_db_session,
    height_context,
    monkeypatch,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    reference = {**REFERENCE, "floor_z_cm": -40}
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", json.dumps([reference]))
    get_settings.cache_clear()
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), height_context["now"])
    assert snapshot.dataState == "UNAVAILABLE" and snapshot.summary.loadPercent is None
    for second in range(5, 10):
        _, snapshot = await frame(client, db, height_context, second, 50)
    assert snapshot.summary.loadPercent == 45
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=10), 60
    )
    assert history.samples[0].valuePercent is None
    assert history.samples[0].averagePercent is None
    assert history.samples[0].inputSource == "MIXED"
    assert "FILL_METHOD_CHANGED_WITHIN_BUCKET" in history.samples[0].reasonCodes


async def test_configuration_arrives_late_then_height_warms_up(
    client, test_db_session, height_context
):
    db = test_db_session
    c = await prepare(client, db, configured=False)
    _, snapshot = await frame(client, db, height_context, 0, 50)
    assert snapshot.summary.loadPercent is None
    assert "HEIGHT_CONFIGURATION_UNAVAILABLE" in snapshot.reasonCodes
    assert (await send(client, c)).status_code == 201
    for second in range(1, 6):
        _, snapshot = await frame(client, db, height_context, second, 50)
    assert snapshot.summary.loadPercent == 50


async def test_edge_mode_can_switch_to_height_without_mixing_filter_samples(
    client,
    test_db_session,
    height_context,
    monkeypatch,
):
    db = test_db_session
    await prepare(client, db)
    monkeypatch.setenv("EDGE_V2_HEIGHT_FILL_ENABLED", "false")
    get_settings.cache_clear()
    for second in range(5):
        height_context["now"] = START + timedelta(seconds=second)
        a = assessment(f"edge-{second}", 0.8, height_context["now"])
        a["config_revision"] = "cfg-1"
        assert (await send(client, a)).status_code == 201
    assert (
        await build_snapshot(db, monitoring_source("pit-demo"), height_context["now"])
    ).summary.loadPercent == 80
    monkeypatch.setenv("EDGE_V2_HEIGHT_FILL_ENABLED", "true")
    get_settings.cache_clear()
    for second in range(5, 10):
        _, snapshot = await frame(client, db, height_context, second, 50)
        assert snapshot.summary.loadPercent == (50 if second == 9 else None)
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=10), 60
    )
    assert history.samples[0].inputSource == "MIXED" and not history.samples[0].valid


async def test_unverified_height_never_falls_back_to_an_enabled_edge_value(
    client,
    test_db_session,
    height_context,
    monkeypatch,
):
    db = test_db_session
    await prepare(client, db)
    monkeypatch.setenv("EDGE_V2_HEIGHT_REFERENCES", json.dumps([{**REFERENCE, "verified": False}]))
    get_settings.cache_clear()
    for second in range(8):
        await frame(client, db, height_context, second, 95, wire=False)
        a = assessment(f"enabled-edge-{second}", 0.95, height_context["now"])
        a["config_revision"] = "cfg-1"
        assert (await send(client, a)).status_code == 201
        snapshot = await build_snapshot(db, monitoring_source("pit-demo"), height_context["now"])
        assert snapshot.assessment.fill_estimate.ratio == 0.95
        assert snapshot.summary.loadPercent is None
        assert snapshot.summary.inputSource == "HEIGHT_SECTIONS"
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 0


@pytest.mark.parametrize("retain_metadata", [True, False])
async def test_observation_gap_restarts_filter_and_partial_history_keeps_honest_metadata(
    client,
    test_db_session,
    height_context,
    retain_metadata,
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    for second in range(10, 15):
        _, snapshot = await frame(client, db, height_context, second, 80)
        assert snapshot.summary.loadPercent == (80 if second == 14 else None)
    if not retain_metadata:
        row = await db.get(EdgeV2Minute, ("site-demo", "edge-demo", START))
        legacy = copy.deepcopy(row.payload)
        legacy.pop("metadata", None)
        row.payload = legacy
        await db.commit()
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=4.5), 60
    )
    sample = history.samples[0]
    assert sample.valuePercent == 50 and sample.valid
    if retain_metadata:
        assert sample.generatedAt == sample.observedAt == START + timedelta(seconds=4)
        assert sample.observationId is not None
        assert "COMPACT_SUBMINUTE_METADATA_NOT_RETAINED" not in sample.reasonCodes
    else:
        assert sample.generatedAt is sample.observedAt is sample.observationId is None
        assert "COMPACT_SUBMINUTE_METADATA_NOT_RETAINED" in sample.reasonCodes
    assert sample.inputSource == "HEIGHT_SECTIONS"


async def test_new_boot_invalidates_ready_window_immediately(
    client, test_db_session, height_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, height_context, second, 50)
    now = START + timedelta(seconds=5)
    height_context["now"] = now
    s = status("new-boot", now, 0)
    s.update(config_revision="cfg-1", orchestrator_instance_id="new-orchestrator")
    assert (await send(client, s)).status_code == 201
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), now)
    assert snapshot.summary.loadPercent is None and snapshot.dataState == "UNKNOWN"
    assert (await db.get(EdgeV2Filter, ("site-demo", "edge-demo"))).payload == {}


def test_height_fill_is_opt_in_and_requires_explicit_references():
    assert not Settings(_env_file=None).EDGE_V2_HEIGHT_FILL_ENABLED
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, EDGE_V2_HEIGHT_FILL_ENABLED=True)
