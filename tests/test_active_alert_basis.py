"""Expose the exact snapshot basis without reclassifying or persisting alarms."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_edge_v2_operations import operations_config as operations_config
from test_edge_v2_operations import tick
from test_monitoring_v2 import NOW, assessment, login, send

from src.app.main import create_app
from src.app.models.log import AlertLog
from src.app.services.active_alerts_v2_service import _fill_alerts, active_fill_alerts
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source


@pytest.mark.parametrize("ratio,state", [(0.5, "NORMAL"), (0.8, "WARNING"), (0.99, "CRITICAL")])
async def test_basis_matches_same_snapshot_and_preserves_alarms(
    client, test_db_session, ratio, state
):
    db = test_db_session
    await tick(client, db, ratio, 0)
    source = monitoring_source("pit-demo")
    snap = await build_snapshot(db, source, NOW)
    before = await db.scalar(select(func.count()).select_from(AlertLog))
    result = await active_fill_alerts(db, source, NOW)
    assert result.evaluationState == snap.operations.effectiveState == state
    assert result.basis.snapshotId == snap.snapshotId
    assert result.basis.measuredAt == snap.measuredAt
    assert result.basis.lastEvaluatedAt == snap.operations.lastEvaluatedAt
    assert result.basis.evaluationSource == "CURRENT_OBSERVATION"
    assert result.basis.reasonCodes == snap.reasonCodes
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == before


async def test_warming_keeps_past_normal_separate_from_current_unknown(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.5, 0)
    source = monitoring_source("pit-demo")
    snap = await build_snapshot(db, source, NOW)
    snap.dataState = "UNKNOWN"
    snap.summary.filterState = "WARMING"
    snap.summary.loadPercent = None
    snap.reasonCodes = ["FILTER_WARMING"]
    snap.operations.effectiveState = "UNKNOWN"
    snap.operations.evaluationSource = None
    result = await _fill_alerts(db, source, snap, NOW)
    assert result.evaluationState == "UNKNOWN" and result.items == []
    assert result.basis.lastKnownState == "NORMAL"
    assert result.basis.filterState == "WARMING"
    assert result.basis.reasonCodes == ["FILTER_WARMING"]


async def test_pending_and_expired_evaluation_keep_original_times(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    source = monitoring_source("pit-demo")
    first = await active_fill_alerts(db, source, NOW)
    await send(client, assessment("basis-pending", ratio=0.2, when=NOW + timedelta(seconds=1)))
    pending = await active_fill_alerts(db, source, NOW + timedelta(seconds=1))
    assert pending.evaluationState == "CRITICAL"
    assert pending.basis.snapshotId != first.basis.snapshotId
    assert pending.basis.evaluationSource == "RECENT_PERSISTED"
    assert pending.basis.measuredAt == NOW + timedelta(seconds=1)
    assert pending.basis.lastEvaluatedAt == NOW
    expired = await active_fill_alerts(db, source, NOW + timedelta(seconds=3))
    assert expired.evaluationState == "UNKNOWN"
    assert expired.basis.evaluationSource is None
    assert expired.basis.lastKnownState == "CRITICAL"


@pytest.mark.parametrize("scope", ["FILL_OPERATIONS", "ALL"])
async def test_api_exposes_basis_when_no_operation_exists(client, scope):
    await login(client)
    response = await client.get(
        "/api/v2/monitoring/active-alerts", params={"pit_id": "pit-demo", "scope": scope}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluationState"] == "UNKNOWN"
    assert body["basis"]["snapshotId"]
    assert body["basis"]["lastKnownState"] is None
    assert body["basis"]["lastEvaluatedAt"] is None


def test_basis_is_required_in_actual_response_schema():
    schema = create_app().openapi()
    ref = schema["paths"]["/api/v2/monitoring/active-alerts"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    page = schema["components"]["schemas"][ref["$ref"].rsplit("/", 1)[-1]]
    assert "basis" in page["required"]
