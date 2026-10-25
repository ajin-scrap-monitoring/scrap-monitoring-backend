"""Actual recovery evidence, historical unknowns, and unchanged safety decisions."""

from datetime import datetime, timedelta

import pytest
from test_edge_v2_operations import operations_config, tick  # noqa: F401
from test_monitoring_v2 import NOW, login

from src.app.models.log import AlertLog


@pytest.fixture(autouse=True)
def lifecycle_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            at = NOW + timedelta(seconds=5)
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.alert_lifecycle_service.datetime", Clock)


async def test_old_alert_without_lifecycle_evidence_is_unknown(client, test_db_session):
    db = test_db_session
    db.add(
        AlertLog(
            pit_id="pit-demo",
            triggered_at=NOW - timedelta(seconds=10),
            fill_ratio_percent=95,
            channel="LOG",
            status="RECORDED",
            message="Synthetic",
        )
    )
    await db.commit()
    data = (await client.get("/api/v1/events?pit_id=pit-demo&category=alert")).json()
    item = data["items"][0]
    assert item["status"] == "unknown"
    assert item["resolvedAt"] is None
    assert item["statusBasis"] == "UNVERIFIED"


async def test_valid_recovery_preserves_confirmation_and_observation_times(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.91, -4)
    await tick(client, db, 0.88, -3)
    await tick(client, db, 0.85, -2)
    data = (await client.get("/api/v1/events?pit_id=pit-demo&category=alert")).json()
    item = data["items"][0]
    assert item["status"] == "resolved"
    assert item["resolvedAt"] is not None
    assert datetime.fromisoformat(item["resolvedAt"]) == NOW - timedelta(seconds=2)
    assert item["resolutionObservedAt"] == item["resolvedAt"]
    assert item["statusBasis"] == "PERSISTED_RESOLUTION"
    await login(client)
    v2 = (await client.get("/api/v2/monitoring/alerts?pit_id=pit-demo")).json()["items"][0]
    assert v2.get("lifecycle", {}).get("state") == "RESOLVED"
    assert v2["lifecycle"]["resolvedAt"] == item["resolvedAt"]


async def states(db, seconds=0, pit="pit-demo"):
    from sqlalchemy import select

    from src.app.services.alert_lifecycle_service import alert_lifecycles

    ids = (await db.scalars(select(AlertLog.id).where(AlertLog.pit_id == "pit-demo"))).all()
    return await alert_lifecycles(db, pit, list(ids), NOW + timedelta(seconds=seconds))


async def send_clocked(client, db, payload):
    from sqlalchemy import update
    from test_monitoring_v2 import send

    from src.app.models.edge_v2 import EdgeV2Message

    await send(client, payload)
    await db.execute(
        update(EdgeV2Message)
        .where(EdgeV2Message.message_id == payload["message_id"])
        .values(received_at=datetime.fromisoformat(payload["generated_at"]))
    )
    await db.commit()


async def test_active_hysteresis_and_reads_never_create_or_resolve_records(client, test_db_session):
    from sqlalchemy import func, select

    from src.app.models.edge_v2_projection import EdgeV2Transition

    db = test_db_session
    await tick(client, db, 0.91, 0)
    initial = await db.scalar(select(func.count()).select_from(EdgeV2Transition))
    assert [s.state for s in (await states(db)).values()] == ["ACTIVE"]
    assert [s.state for s in (await states(db, 1)).values()] == ["ACTIVE"]
    assert await db.scalar(select(func.count()).select_from(EdgeV2Transition)) == initial
    await tick(client, db, 0.88, 1)
    assert [s.state for s in (await states(db, 1)).values()] == ["ACTIVE"]
    assert [s.state for s in (await states(db, 31)).values()] == ["UNKNOWN"]
    assert [s.state for s in (await states(db, 1, pit="other-pit")).values()] == ["UNKNOWN"]


async def test_invalid_input_and_later_valid_recovery_cannot_fill_an_unknown_gap(
    client, test_db_session
):
    from test_monitoring_v2 import assessment, send

    from src.app.services.edge_v2_operations import EdgeV2OperationsWorker

    db = test_db_session
    await tick(client, db, 0.91, 0)
    invalid = assessment("invalid-lifecycle", when=NOW + timedelta(seconds=1))
    invalid.update(fill_estimate=None, decision_state="UNAVAILABLE", fill_state="UNAVAILABLE")
    await send(client, invalid)
    await EdgeV2OperationsWorker().run_once(db, NOW + timedelta(seconds=1))
    assert [s.state for s in (await states(db, 1)).values()] == ["UNKNOWN"]
    await tick(client, db, 0.85, 2)
    result = list((await states(db, 2)).values())[0]
    assert result.state == "UNKNOWN" and result.resolvedAt is None


async def test_data_gap_does_not_invent_the_first_recovery_time(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.91, 0)
    await tick(client, db, 0.85, 31)
    result = list((await states(db, 31)).values())[0]
    assert result.state == "UNKNOWN" and result.resolvedAt is None


@pytest.mark.parametrize("change", ["method", "boot", "configuration"])
async def test_changed_context_cannot_resolve_an_old_episode(client, test_db_session, change):
    from test_monitoring_v2 import assessment, status

    from src.app.services.edge_v2_operations import EdgeV2OperationsWorker
    from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source

    db = test_db_session
    await tick(client, db, 0.91, 0)
    report = status("new-boot-status", NOW + timedelta(seconds=1), 20000)
    report["clock"]["state"] = "SYNCED"
    data = assessment("new-method-state", 0.85, NOW + timedelta(seconds=1))
    if change == "boot":
        report["orchestrator_instance_id"] = "another-boot"
    elif change == "method":
        data["fill_estimate"]["method_version"] = "another-method"
    else:
        report["config_revision"] = data["config_revision"] = "another-config"
    await send_clocked(client, db, report)
    await send_clocked(client, db, data)
    await EdgeV2OperationsWorker().run_once(db, NOW + timedelta(seconds=1))
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), NOW + timedelta(seconds=1))
    assert snapshot.dataState == "FRESH" and snapshot.operations.effectiveState == "WARNING"
    assert [s.state for s in (await states(db, 1)).values()] == ["UNKNOWN"]


async def test_threshold_change_and_disabled_operations_are_not_recovery(
    client, test_db_session, monkeypatch
):
    from src.app.core.config import get_settings
    from src.app.models.config import PitConfig

    db = test_db_session
    await tick(client, db, 0.91, 0)
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "false")
    get_settings.cache_clear()
    assert [s.state for s in (await states(db)).values()] == ["UNKNOWN"]
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "true")
    get_settings.cache_clear()
    db.add(
        PitConfig(id="pit-demo", name="Synthetic changed threshold", critical_threshold_percent=95)
    )
    await db.commit()
    await tick(client, db, 0.85, 1)
    assert [s.state for s in (await states(db, 1)).values()] == ["UNKNOWN"]


async def test_cooldown_realerts_share_evidence_but_new_episodes_are_separate(
    client, test_db_session
):
    from sqlalchemy import func, select

    from src.app.models.edge_v2_projection import EdgeV2Transition

    db = test_db_session
    for second in range(0, 1801, 20):
        await tick(client, db, 0.91, second)
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 2
    await tick(client, db, 0.85, 1802)
    ended = await states(db, 1802)
    assert len(ended) == 2 and all(s.state == "RESOLVED" for s in ended.values())
    assert len({s.resolvedAt for s in ended.values()}) == 1
    assert (
        await db.scalar(
            select(func.count())
            .select_from(EdgeV2Transition)
            .where(EdgeV2Transition.kind == "ALERT_RESOLVED")
        )
        == 1
    )
    # A new critical interval within cooldown has no new notification or old-alert reassignment.
    await tick(client, db, 0.91, 1803)
    assert await db.scalar(select(func.count()).select_from(AlertLog)) == 2
    assert all(s.state == "RESOLVED" for s in (await states(db, 1803)).values())


async def test_recovery_commit_failure_and_replay_preserve_one_durable_resolution(
    client, test_db_session, monkeypatch
):
    from sqlalchemy import func, select
    from sqlalchemy.exc import OperationalError
    from test_monitoring_v2 import assessment, status

    from src.app.models.edge_v2_projection import EdgeV2Transition
    from src.app.services.edge_v2_operations import EdgeV2OperationsWorker

    db = test_db_session
    await tick(client, db, 0.91, 0)
    await send_clocked(client, db, status("recovery-status", NOW + timedelta(seconds=1), 20001))
    await send_clocked(client, db, assessment("recovery-state", 0.85, NOW + timedelta(seconds=1)))
    original = db.commit

    async def fail():
        raise OperationalError("synthetic commit", {}, Exception("synthetic"))

    monkeypatch.setattr(db, "commit", fail)
    with pytest.raises(OperationalError):
        await EdgeV2OperationsWorker().run_once(db, NOW + timedelta(seconds=1))
    assert (
        await db.scalar(
            select(func.count())
            .select_from(EdgeV2Transition)
            .where(EdgeV2Transition.kind == "ALERT_RESOLVED")
        )
        == 0
    )
    monkeypatch.setattr(db, "commit", original)
    await EdgeV2OperationsWorker().run_once(db, NOW + timedelta(seconds=1))
    await EdgeV2OperationsWorker().run_once(db, NOW + timedelta(seconds=1))
    assert [s.state for s in (await states(db, 1)).values()] == ["RESOLVED"]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(EdgeV2Transition)
            .where(EdgeV2Transition.kind == "ALERT_RESOLVED")
        )
        == 1
    )


async def test_unrelated_or_malformed_evidence_cannot_claim_recovery(client, test_db_session):
    from sqlalchemy import select

    from src.app.models.edge_v2_projection import EdgeV2Transition

    db = test_db_session
    await tick(client, db, 0.91, 0)
    created = await db.scalar(
        select(EdgeV2Transition).where(EdgeV2Transition.kind == "ALERT_CREATED")
    )
    original = dict(created.payload)
    db.add_all(
        [
            EdgeV2Transition(
                site_id="another-site",
                edge_id=created.edge_id,
                occurred_at=NOW + timedelta(seconds=1),
                kind="ALERT_RESOLVED",
                payload={
                    **original,
                    "resolutionObservedAt": (NOW + timedelta(seconds=1)).isoformat(),
                },
            ),
            EdgeV2Transition(
                site_id=created.site_id,
                edge_id=created.edge_id,
                occurred_at=NOW + timedelta(seconds=1),
                kind="ALERT_CREATED",
                payload={**original, "alertId": "not-an-integer"},
            ),
            EdgeV2Transition(
                site_id=created.site_id,
                edge_id=created.edge_id,
                occurred_at=NOW + timedelta(seconds=100),
                kind="ALERT_RESOLVED",
                payload={
                    **original,
                    "resolutionObservedAt": (NOW + timedelta(seconds=100)).isoformat(),
                },
            ),
        ]
    )
    await db.commit()
    assert [s.state for s in (await states(db, 1)).values()] == ["ACTIVE"]


async def test_legacy_invalid_measurement_without_recovery_is_unknown(client, test_db_session):
    from src.app.models.metric import ScrapMetric

    test_db_session.add(
        ScrapMetric(
            pit_id="pit-demo",
            measured_at=NOW - timedelta(seconds=1),
            is_valid=False,
            overall_quality="INVALID",
            state="ERROR",
        )
    )
    await test_db_session.commit()
    item = (await client.get("/api/v1/events?pit_id=pit-demo&category=error")).json()["items"][0]
    assert item["status"] == "unknown" and item["resolvedAt"] is None
    assert item["statusBasis"] == "UNVERIFIED"


async def test_read_notification_is_not_alarm_recovery(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.91, -1)
    _, session = await login(client)
    before = (await client.get("/api/v2/monitoring/alerts?pit_id=pit-demo")).json()["items"][0]
    assert before["lifecycle"]["state"] == "ACTIVE"
    response = await client.patch(
        "/api/v1/notifications/" + before["notificationId"],
        json={"read": True},
        headers={"X-CSRF-Token": session.csrfToken},
    )
    assert response.status_code == 200
    after = (await client.get("/api/v2/monitoring/alerts?pit_id=pit-demo")).json()["items"][0]
    assert after["readAt"] is not None
    assert after["lifecycle"]["state"] == "ACTIVE"
    assert after["lifecycle"]["resolvedAt"] is None


async def test_recorded_recovery_survives_removed_current_assignment(
    client, test_db_session, monkeypatch
):
    from src.app.core.config import get_settings

    db = test_db_session
    await tick(client, db, 0.91, 0)
    await tick(client, db, 0.85, 1)
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", "[]")
    get_settings.cache_clear()
    assert [s.state for s in (await states(db, 1000)).values()] == ["RESOLVED"]
    item = (await client.get("/api/v1/events?pit_id=pit-demo&category=alert")).json()["items"][0]
    assert item["status"] == "resolved" and item["resolvedAt"] is not None
