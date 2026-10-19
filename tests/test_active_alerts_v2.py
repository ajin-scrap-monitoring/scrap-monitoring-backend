"""Active alarms follow current judgment, not unread historical notification rows."""

from datetime import timedelta

from sqlalchemy import func, select
from test_edge_v2_operations import operations_config, tick  # noqa: F401
from test_monitoring_v2 import NOW, assessment, login, send

from src.app.core.config import get_settings
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog
from src.app.services.active_alerts_v2_service import active_fill_alerts
from src.app.services.monitoring_v2_service import monitoring_source

URL = "/api/v2/monitoring/active-alerts?pit_id=pit-demo"


async def active(db, seconds=0):
    return await active_fill_alerts(
        db, monitoring_source("pit-demo"), NOW + timedelta(seconds=seconds)
    )


async def test_active_alerts_authentication_and_disabled_is_unknown(client, monkeypatch):
    assert (await client.get(URL)).status_code == 401
    await login(client)
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "false")
    get_settings.cache_clear()
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["scope"] == "FILL_OPERATIONS"
    assert result["evaluationState"] == "UNKNOWN"
    assert result["reasonCodes"] == ["OPERATIONS_DISABLED"]
    assert result["items"] == []
    assert (await client.get(URL.replace("pit-demo", "other"))).status_code == 404


async def test_current_critical_hysteresis_recovery_and_no_read_side_effects(
    client, test_db_session
):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    initial = await db.scalar(select(func.count()).select_from(AlertLog))
    result = await active(db)
    assert result.evaluationState == "CRITICAL"
    alert = result.items[0]
    assert alert.type == "collection_required"
    assert alert.severity == "error"
    assert alert.loadPercent == 99
    assert alert.notificationId is not None
    assert alert.evaluatedAt == NOW
    # The latest number is shown, even while hysteresis keeps the critical state.
    await tick(client, db, 0.88, 1)
    result = await active(db, 1)
    assert result.items[0].loadPercent == 88
    assert result.items[0].id == alert.id
    await tick(client, db, 0.85, 2)
    result = await active(db, 2)
    assert result.evaluationState == "WARNING"
    assert result.items[0].type == "pre_collection_alert"
    assert result.items[0].notificationId is None
    await tick(client, db, 0.7, 3)
    result = await active(db, 3)
    assert result.evaluationState == "NORMAL" and result.items == []
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == initial == 1
    log = await db.scalar(select(AlertLog))
    assert log.read_at is None


async def test_warning_respects_saved_pre_alert_switch(client, test_db_session):
    db = test_db_session
    db.add(PitConfig(id="pit-demo", name="Synthetic", total_depth_cm=100))
    await db.flush()
    db.add(AlertPolicy(pit_id="pit-demo", pre_alert_enabled=False))
    await db.commit()
    await tick(client, db, 0.8, 0)
    result = await active(db)
    assert result.evaluationState == "WARNING"
    assert result.items == []
    assert result.reasonCodes == ["PRE_COLLECTION_ALERT_DISABLED"]


async def test_stale_data_is_unknown_but_brief_pending_preserves_recent_alarm(
    client, test_db_session
):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    stale = await active(db, 31)
    assert stale.evaluationState == "UNKNOWN" and stale.items == []
    await send(client, assessment("pending", ratio=0.99, when=NOW + timedelta(seconds=1)))
    pending = await active(db, 1)
    assert pending.evaluationState == "CRITICAL"
    assert pending.items[0].evaluationSource == "RECENT_PERSISTED"
    assert pending.items[0].evaluatedAt == NOW
    assert pending.items[0].measuredAt == NOW + timedelta(seconds=1)
    assert "OPERATIONS_EVALUATION_PENDING" in pending.reasonCodes


async def test_invalid_data_reports_error_not_no_alarm(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    value = assessment("invalid", when=NOW + timedelta(seconds=1))
    value.update(fill_estimate=None, fill_state="UNAVAILABLE", decision_state="UNAVAILABLE")
    await send(client, value)
    result = await active(db, 1)
    assert result.evaluationState == "ERROR"
    assert result.items == []
    assert result.reasonCodes
