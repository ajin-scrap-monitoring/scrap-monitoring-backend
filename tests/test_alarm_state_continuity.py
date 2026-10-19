"""Continuous readings cannot hide a real outage or advance the durable alarm cursor."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from test_edge_v2_operations import operations_config, tick  # noqa: F401
from test_monitoring_v2 import NOW, assessment, send, status

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2Operation
from src.app.models.log import AlertLog
from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from tests import test_simulation_height as simulation
from tests.test_height_fill_pipeline import height_context as height_context  # noqa: F401
from tests.test_sensor_angle_height import angle_context as angle_context  # noqa: F401


async def snapshot(db, when):
    return await build_snapshot(db, monitoring_source("pit-demo"), when)


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "ratio,expected", [(0.50, "NORMAL"), (0.80, "WARNING"), (0.99, "CRITICAL")]
)
async def test_thirty_pending_reads_remain_stable_without_writing_alarms(
    client, test_db_session, configured, ratio, expected
):
    db = test_db_session
    if configured:
        db.add(
            PitConfig(
                id="pit-demo",
                name="Synthetic",
                total_depth_cm=100,
                warning_threshold_percent=75,
                critical_threshold_percent=90,
                updated_at=NOW - timedelta(minutes=1),
            )
        )
        await db.commit()
    for second in range(3):
        await tick(client, db, ratio, second)
        before = await db.get(EdgeV2Operation, "pit-demo")
        cursor = before.message_id
        for part in range(1, 11):
            when = NOW + timedelta(seconds=second + part / 20)
            await send(client, assessment(f"pending-{second}-{part}", ratio - 0.01, when))
            result = await snapshot(db, when)
            assert result.dataState == "FRESH"
            assert result.summary.loadPercent == pytest.approx((ratio - 0.01) * 100)
            assert result.operations.effectiveState == expected
            assert result.operations.evaluationSource == "RECENT_PERSISTED"
            assert result.operations.lastEvaluatedAt == NOW + timedelta(seconds=second)
            assert (await db.get(EdgeV2Operation, "pit-demo")).message_id == cursor
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == (
        1 if expected == "CRITICAL" else 0
    )


async def test_three_second_boundary_reveals_stalled_worker_and_recovery(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    await send(client, assessment("pending", 0.2, NOW + timedelta(seconds=1)))
    recent = await snapshot(db, NOW + timedelta(seconds=2, milliseconds=999))
    assert recent.operations.effectiveState == "CRITICAL"
    delayed = await snapshot(db, NOW + timedelta(seconds=3))
    assert delayed.dataState == "FRESH" and delayed.summary.loadPercent == 20
    assert delayed.operations.effectiveState == "UNKNOWN"
    assert delayed.operations.evaluationSource is None
    assert "OPERATIONS_EVALUATION_UNAVAILABLE" in delayed.reasonCodes
    await EdgeV2OperationsWorker().run_once(db, NOW + timedelta(seconds=3))
    resumed = await snapshot(db, NOW + timedelta(seconds=3))
    assert resumed.operations.effectiveState == "NORMAL"
    assert resumed.operations.evaluationSource == "CURRENT_OBSERVATION"
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 1


@pytest.mark.parametrize(
    "guard", ["source", "config", "input_mode", "future_cursor", "settings", "missing_receipt"]
)
async def test_incompatible_checkpoint_is_not_reused(client, test_db_session, guard):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    await send(client, assessment("pending", 0.98, NOW + timedelta(seconds=1)))
    row = await db.get(EdgeV2Operation, "pit-demo")
    receipt = await db.scalar(select(EdgeV2Message).where(EdgeV2Message.message_id == "state-0"))
    if guard == "source":
        row.edge_id = "other-device"
    elif guard == "config":
        receipt.config_revision = "old-config"
    elif guard == "input_mode":
        receipt.message_type = "lidar_measurement"
    elif guard == "future_cursor":
        row.observed_at = row.generated_at = NOW + timedelta(seconds=2)
    elif guard == "missing_receipt":
        await db.delete(receipt)
    else:
        db.add(
            PitConfig(
                id="pit-demo",
                name="Synthetic",
                total_depth_cm=100,
                updated_at=NOW + timedelta(milliseconds=500),
            )
        )
    await db.commit()
    result = await snapshot(db, NOW + timedelta(seconds=1))
    assert result.dataState == "FRESH"
    assert result.operations.effectiveState == "UNKNOWN"
    assert result.operations.evaluationSource is None


async def test_invalid_measurement_does_not_keep_recent_critical(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    value = assessment("invalid", when=NOW + timedelta(seconds=1))
    value.update(fill_estimate=None, fill_state="UNAVAILABLE", decision_state="UNAVAILABLE")
    await send(client, value)
    result = await snapshot(db, NOW + timedelta(seconds=1))
    assert result.operations.effectiveState == "ERROR"
    assert result.operations.evaluationSource is None
    assert result.summary.loadPercent is None


async def test_first_observation_requires_a_committed_evaluation(client, test_db_session):
    await send(client, status())
    await send(client, assessment())
    result = await snapshot(test_db_session, NOW)
    assert result.dataState == "FRESH"
    assert result.operations.effectiveState == "UNKNOWN"
    assert result.operations.evaluationSource is None


async def test_shorter_observation_ttl_also_limits_alarm_reuse(
    client, test_db_session, monkeypatch
):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    monkeypatch.setenv("EDGE_V2_ASSESSMENT_TTL_SECONDS", "1")
    get_settings.cache_clear()
    now = NOW + timedelta(seconds=1, milliseconds=100)
    await send(client, status("fresh-status", now, 10001))
    await db.execute(
        update(EdgeV2Message)
        .where(EdgeV2Message.message_id == "fresh-status")
        .values(received_at=now)
    )
    await db.commit()
    await send(client, assessment("pending", 0.98, now))
    result = await snapshot(db, now)
    assert result.dataState == "FRESH"
    assert result.operations.effectiveState == "UNKNOWN"
    assert result.operations.evaluationSource is None


async def test_received_height_pending_remains_stable_and_sensor_failure_is_visible(
    client, test_db_session, angle_context, monkeypatch
):
    db = test_db_session
    await simulation.prepare(client, db)
    for second in range(11):
        _, result = await simulation.frame(client, db, angle_context, second, 95)
    assert result.operations.effectiveState == "CRITICAL"
    cursor = (await db.get(EdgeV2Operation, "pit-demo")).message_id

    async def paused_worker(*args, **kwargs):
        pass

    monkeypatch.setattr(simulation, "process_source", paused_worker)
    for second in (11, 12):
        _, pending = await simulation.frame(client, db, angle_context, second, 96)
        assert pending.dataState == "FRESH"
        assert pending.summary.simulated
        assert pending.operations.effectiveState == "CRITICAL"
        assert pending.operations.evaluationSource == "RECENT_PERSISTED"
        assert (await db.get(EdgeV2Operation, "pit-demo")).message_id == cursor
    _, failed = await simulation.frame(client, db, angle_context, 13, 96, sensor_failure=True)
    assert failed.operations.effectiveState in ("UNKNOWN", "ERROR")
    assert failed.operations.evaluationSource is None
    assert failed.summary.loadPercent is None
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 1
