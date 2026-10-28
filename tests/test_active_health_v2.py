"""Current evidence, server start times, scope isolation and read-only dashboard queries."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_edge_v2_operations import operations_config, tick  # noqa: F401
from test_monitoring_v2 import NOW, assessment, login, monitoring_config, send, status  # noqa: F401

from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.services.active_alerts_v2_service import active_fill_alerts
from src.app.services.active_health_service import TRACKER_KIND, health_conditions, record_starts
from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
from src.app.services.monitoring_v2_service import monitoring_source

URL = "/api/v2/monitoring/active-alerts"


async def page(db, seconds=0, health=True):
    return await active_fill_alerts(
        db, monitoring_source("pit-demo"), NOW + timedelta(seconds=seconds), include_health=health
    )


async def faulty(client, db, seconds=0, sequence=1, *, instance="orch-demo"):
    when = NOW + timedelta(seconds=seconds)
    report = status(f"fault-{sequence}", when, sequence)
    report["orchestrator_instance_id"] = instance
    report["overall_state"] = "MEASUREMENT_UNAVAILABLE"
    report["reason_codes"] = ["SYNTHETIC_SENSOR_FAILURE"]
    report["sensors"][0].update(data_state="UNAVAILABLE", reason_codes=["SCAN_UNAVAILABLE"])
    await send(client, report)
    value = assessment(f"invalid-{sequence}", when=when)
    value.update(
        fill_estimate=None,
        decision_state="UNAVAILABLE",
        fill_state="UNAVAILABLE",
        reason_codes=["INPUT_UNAVAILABLE"],
    )
    await send(client, value)
    await EdgeV2OperationsWorker().run_once(db, when)


async def test_opt_in_api_auth_scope_and_unconfigured_camera(client, test_db_session):
    assert (await client.get(URL, params={"pit_id": "pit-demo", "scope": "ALL"})).status_code == 401
    await login(client)
    response = await client.get(URL, params={"pit_id": "pit-demo", "scope": "ALL"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["scope"] == "ALL"
    assert any(
        x["componentKind"] == "CAMERA" and x["state"] == "UNSUPPORTED" for x in data["coverage"]
    )
    assert all(x["startedAt"] is None for x in data["items"])
    assert (await client.get(URL, params={"pit_id": "pit-demo", "scope": "bad"})).status_code == 422
    assert (await client.get(URL, params={"pit_id": "other", "scope": "ALL"})).status_code == 404
    plain = (await client.get(URL, params={"pit_id": "pit-demo"})).json()
    assert plain["scope"] == "FILL_OPERATIONS" and plain["items"] == []
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Latest)) == 0
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_fill_start_is_stable_and_separate_from_latest_evaluation(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    first = (await page(db, health=False)).items[0]
    assert first.startedAt == NOW and first.startBasis == "SERVER_EVALUATION"
    await tick(client, db, 0.99, 1)
    again = (await page(db, 1, health=False)).items[0]
    assert again.startedAt == NOW and again.evaluatedAt == NOW + timedelta(seconds=1)
    await tick(client, db, 0.85, 2)
    warning = (await page(db, 2, health=False)).items[0]
    assert warning.type == "pre_collection_alert" and warning.startedAt == NOW + timedelta(
        seconds=2
    )
    await tick(client, db, 0.7, 3)
    assert (await page(db, 3)).items == []
    await tick(client, db, 0.99, 4)
    assert (await page(db, 4, health=False)).items[0].startedAt == NOW + timedelta(seconds=4)


async def test_active_device_sensor_measurement_and_recovery(client, test_db_session):
    db = test_db_session
    await faulty(client, db)
    data = await page(db)
    by_kind = {item.componentKind: item for item in data.items}
    assert set(by_kind) == {"EDGE", "LIDAR", "MEASUREMENT"}
    assert by_kind["LIDAR"].componentId == "lidar-a"
    assert all(x.startedAt == NOW for x in data.items)
    await faulty(client, db, seconds=1, sequence=2)
    assert all(x.startedAt == NOW for x in (await page(db, 1)).items)
    await tick(client, db, 0.7, 2)
    assert (await page(db, 2)).items == []
    await faulty(client, db, seconds=3, sequence=10004)
    assert all(x.startedAt == NOW + timedelta(seconds=3) for x in (await page(db, 3)).items)


async def test_old_status_is_not_a_current_sensor_fault(client, test_db_session):
    await faulty(client, test_db_session)
    data = await page(test_db_session, 31)
    assert len(data.items) == 1
    item = data.items[0]
    assert item.componentKind == "EDGE" and item.state == "STATUS_UNKNOWN"
    assert item.evidenceSource == "STATUS_FRESHNESS" and item.startedAt is None
    assert all(x.state == "UNKNOWN" for x in data.coverage if x.componentKind in ("EDGE", "LIDAR"))


async def test_late_tracker_and_other_pit_cannot_supply_start_time(client, test_db_session):
    db = test_db_session
    await faulty(client, db)
    tracker = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", TRACKER_KIND))
    tracker.payload = {**tracker.payload, "pitId": "other"}
    await db.commit()
    assert all(x.startedAt is None for x in (await page(db)).items)


async def test_reboot_resets_activation_evidence(client, test_db_session):
    await faulty(client, test_db_session)
    await faulty(client, test_db_session, seconds=1, sequence=2, instance="new-instance")
    assert all(
        x.startedAt == NOW + timedelta(seconds=1) for x in (await page(test_db_session, 1)).items
    )


async def test_gap_and_new_worker_do_not_join_unobserved_time(client, test_db_session):
    db = test_db_session
    await faulty(client, db)
    await faulty(client, db, seconds=31, sequence=2)
    assert all(x.startedAt == NOW + timedelta(seconds=31) for x in (await page(db, 31)).items)


async def test_get_never_mutates_tracking_or_acknowledges_alerts(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    tracker = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", TRACKER_KIND))
    before = dict(tracker.payload)
    count = await db.scalar(select(func.count()).select_from(AlertLog))
    for _ in range(3):
        await page(db)
    assert tracker.payload == before
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == count
    assert (await db.scalar(select(AlertLog))).read_at is None
    assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("scenario", ["preparing", "missing_sensor", "stale_scan", "future_status"])
async def test_health_coverage_distinguishes_preparing_unknown_and_stale(
    client, test_db_session, scenario
):
    snapshot = await tick(client, test_db_session, 0.7, 0)
    report = snapshot.deviceStatus.snapshot
    if scenario == "preparing":
        report.overall_state = "DEGRADED"
        report.reason_codes = ["INITIALIZING"]
        for sensor in report.sensors:
            sensor.data_state = "RECOVERING"
            sensor.reason_codes = ["SCAN_RECOVERING"]
        snapshot.dataState = "UNKNOWN"
        snapshot.reasonCodes = ["FILTER_WARMING"]
    elif scenario == "missing_sensor":
        report.sensors = report.sensors[:1]
    elif scenario == "stale_scan":
        report.sensors[0].last_scan_at = NOW - timedelta(seconds=31)
    else:
        report.generated_at = NOW + timedelta(seconds=60)
    items, coverage = health_conditions(snapshot, NOW)
    if scenario == "preparing":
        assert items == []
        assert all(x.state == "UNKNOWN" for x in coverage if x.componentKind == "LIDAR")
    elif scenario == "missing_sensor":
        assert len(items) == 1 and items[0].componentId == "lidar-b" and items[0].state == "UNKNOWN"
    elif scenario == "stale_scan":
        assert len(items) == 1 and items[0].state == "STALE"
    else:
        assert len(items) == 1 and items[0].state == "STATUS_UNKNOWN"


async def test_old_records_without_tracker_keep_unknown_start(client, test_db_session):
    snapshot = await tick(client, test_db_session, 0.99, 0)
    tracker = await test_db_session.get(EdgeV2Latest, ("site-demo", "edge-demo", TRACKER_KIND))
    await test_db_session.delete(tracker)
    await test_db_session.commit()
    item = (await page(test_db_session, health=False)).items[0]
    assert item.startedAt is None and item.startBasis == "UNKNOWN"
    assert snapshot.operations.effectiveState == "CRITICAL"


async def test_tracker_is_bounded_idempotent_and_uses_no_external_service(client, test_db_session):
    snapshot = await tick(client, test_db_session, 0.99, 0)
    assert not await record_starts(test_db_session, snapshot, NOW, "CRITICAL")
    rows = (
        await test_db_session.scalars(select(EdgeV2Latest).where(EdgeV2Latest.kind == TRACKER_KIND))
    ).all()
    assert len(rows) == 1 and len(rows[0].payload["entries"]) <= 5
