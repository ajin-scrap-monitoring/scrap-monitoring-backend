"""A combined response retains its captured measurement while newer data arrives."""

import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_camera_status import camera as camera  # noqa: F401
from test_edge_v2_operations import operations_config as operations_config
from test_edge_v2_operations import tick
from test_monitoring_v2 import NOW, assessment, login, monitoring_config, send  # noqa: F401

from src.app.api import monitoring_v2 as route
from src.app.core.config import get_settings
from src.app.db.session import get_db_session
from src.app.main import create_app
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.models.edge_v2_operations import EdgeV2Operation
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.services import active_alerts_v2_service as alert_service
from src.app.services.active_health_service import TRACKER_KIND
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source

URL = "/api/v2/monitoring/snapshot-with-alerts"
PARAMS = {"pit_id": "pit-demo"}


def assert_shared_basis(body):
    snapshot, alerts = body["snapshot"], body["activeAlerts"]
    basis = alerts["basis"]
    assert alerts["pitId"] == snapshot["pitId"]
    assert alerts["computedAt"] == snapshot["serverTime"]
    assert basis["snapshotId"] == snapshot["snapshotId"]
    assert basis["dataState"] == snapshot["dataState"]
    assert basis["measuredAt"] == snapshot["measuredAt"]
    assert basis["inputSource"] == snapshot["summary"]["inputSource"]
    assert basis["filterState"] == snapshot["summary"]["filterState"]
    assert basis["reasonCodes"] == snapshot["reasonCodes"]
    operations = snapshot["operations"]
    if operations:
        assert alerts["evaluationState"] == operations["effectiveState"]
        assert basis["lastEvaluatedAt"] == operations["lastEvaluatedAt"]
        assert basis["evaluationSource"] == operations["evaluationSource"]
        assert basis["lastKnownState"] == operations["lastKnownState"]
    else:
        assert alerts["evaluationState"] == "UNKNOWN"
        assert basis["lastEvaluatedAt"] is None


async def test_auth_scope_source_and_default_never_probe_camera(client, camera):
    assert (await client.get(URL, params=PARAMS)).status_code == 401
    await login(client)
    for params, expected in [
        ({**PARAMS, "scope": "bad"}, 422),
        ({"pit_id": "unmapped", "scope": "ALL"}, 404),
    ]:
        assert (await client.get(URL, params=params)).status_code == expected
    response = await client.get(URL, params=PARAMS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["activeAlerts"]["scope"] == "FILL_OPERATIONS"
    assert_shared_basis(response.json())
    assert camera.calls == 0


@pytest.mark.parametrize("ratio,state", [(0.5, "NORMAL"), (0.8, "WARNING"), (0.99, "CRITICAL")])
async def test_one_captured_snapshot_supplies_both_display_areas(
    client, test_db_session, monkeypatch, ratio, state
):
    await tick(client, test_db_session, ratio, 0)
    await login(client)
    calls = 0

    async def captured(db, source):
        nonlocal calls
        calls += 1
        return await build_snapshot(db, source, NOW)

    async def forbidden_rebuild(*args, **kwargs):
        raise AssertionError("Alert lookup must reuse the captured snapshot")

    monkeypatch.setattr(route, "build_snapshot", captured)
    monkeypatch.setattr(alert_service, "build_snapshot", forbidden_rebuild)
    response = await client.get(URL, params=PARAMS)
    assert response.status_code == 200
    body = response.json()
    assert_shared_basis(body)
    assert calls == 1 and body["activeAlerts"]["evaluationState"] == state
    assert body["snapshot"]["summary"]["loadPercent"] == ratio * 100
    assert len(body["activeAlerts"]["items"]) == (0 if state == "NORMAL" else 1)
    assert not test_db_session.in_transaction()


async def test_measurement_arriving_between_snapshot_and_alerts_cannot_mix_states(
    client, test_db_session, monkeypatch
):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    await login(client)

    async def captured_before_new_measurement(db, source):
        captured = await build_snapshot(db, source, NOW)
        # A later committed measurement changes the durable operation before
        # the response's alarms are assembled; the captured pair must stay intact.
        await tick(client, db, 0.5, 1)
        return captured

    monkeypatch.setattr(route, "build_snapshot", captured_before_new_measurement)
    body = (await client.get(URL, params=PARAMS)).json()
    assert_shared_basis(body)
    assert body["snapshot"]["summary"]["loadPercent"] == 99
    assert body["activeAlerts"]["evaluationState"] == "CRITICAL"
    assert body["activeAlerts"]["items"][0]["loadPercent"] == 99
    latest = await build_snapshot(db, monitoring_source("pit-demo"), NOW + timedelta(seconds=1))
    assert latest.summary.loadPercent == 50 and latest.operations.effectiveState == "NORMAL"
    assert latest.snapshotId != body["snapshot"]["snapshotId"]


@pytest.mark.parametrize(
    "seconds,state,source",
    [(1, "CRITICAL", "RECENT_PERSISTED"), (3, "UNKNOWN", None), (31, "UNKNOWN", None)],
)
async def test_pending_and_stale_pairs_keep_original_evaluation_times(
    client, test_db_session, monkeypatch, seconds, state, source
):
    await tick(client, test_db_session, 0.99, 0)
    await send(client, assessment("pending-pair", 0.2, NOW + timedelta(seconds=1)))
    await login(client)

    async def captured(db, assigned):
        return await build_snapshot(db, assigned, NOW + timedelta(seconds=seconds))

    monkeypatch.setattr(route, "build_snapshot", captured)
    body = (await client.get(URL, params=PARAMS)).json()
    assert_shared_basis(body)
    assert body["activeAlerts"]["evaluationState"] == state
    assert body["activeAlerts"]["basis"]["evaluationSource"] == source
    assert body["activeAlerts"]["basis"]["lastKnownState"] == "CRITICAL"
    assert body["activeAlerts"]["basis"]["lastEvaluatedAt"] == NOW.isoformat().replace(
        "+00:00", "Z"
    )
    if seconds == 31:
        assert body["snapshot"]["summary"]["loadPercent"] is None


async def test_disabled_operations_remain_unknown_and_null(client, monkeypatch):
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "false")
    get_settings.cache_clear()
    await login(client)
    body = (await client.get(URL, params=PARAMS)).json()
    assert_shared_basis(body)
    assert body["snapshot"]["operations"] is None
    assert body["snapshot"]["summary"]["loadPercent"] is None
    assert body["activeAlerts"]["reasonCodes"] == ["OPERATIONS_DISABLED"]


@pytest.mark.parametrize("status", [200, 503])
async def test_all_scope_camera_has_separate_evidence_without_breaking_fill_pair(
    client, test_db_session, camera, status
):
    await tick(client, test_db_session, 0.99, 0)
    camera.status = status
    if status == 200:
        camera.body.update(status="interrupted", reason_code="FRAME_STALE")
    await login(client)
    response = await client.get(URL, params={**PARAMS, "scope": "ALL"})
    assert response.status_code == 200
    body = response.json()
    assert_shared_basis(body)
    assert body["activeAlerts"]["scope"] == "ALL"
    assert body["activeAlerts"]["evaluationState"] == "CRITICAL"
    camera_item = next(
        x for x in body["activeAlerts"]["items"] if x.get("componentKind") == "CAMERA"
    )
    assert camera_item["evidenceSource"] == (
        "MEDIA_CAMERA_STATUS" if status == 200 else "STATUS_FRESHNESS"
    )
    assert camera_item["state"] == ("INTERRUPTED" if status == 200 else "UNKNOWN")
    assert camera_item["startedAt"] is None
    assert camera.calls == 1 and not test_db_session.in_transaction()


async def test_warning_switch_and_error_state_keep_same_basis(client, test_db_session, monkeypatch):
    db = test_db_session
    db.add(PitConfig(id="pit-demo", name="Synthetic pit"))
    await db.commit()
    db.add(AlertPolicy(pit_id="pit-demo", pre_alert_enabled=False))
    await db.commit()
    await tick(client, db, 0.8, 0)
    await login(client)
    body = (await client.get(URL, params=PARAMS)).json()
    assert_shared_basis(body)
    assert body["activeAlerts"]["evaluationState"] == "WARNING"
    assert body["activeAlerts"]["items"] == []
    assert body["activeAlerts"]["reasonCodes"] == ["PRE_COLLECTION_ALERT_DISABLED"]

    unavailable = assessment("unavailable-pair", when=NOW)
    unavailable.update(fill_estimate=None, fill_state="UNAVAILABLE", decision_state="UNAVAILABLE")
    unavailable["capabilities"]["fill"] = "NOT_VALIDATED"
    # A distinct, newer observed STATE supersedes the previous value.
    when = NOW + timedelta(seconds=1)
    unavailable.update(
        generated_at=when.isoformat(), observed_from=when.isoformat(), observed_to=when.isoformat()
    )
    await send(client, unavailable)

    async def captured(db, source):
        return await build_snapshot(db, source, when)

    monkeypatch.setattr(route, "build_snapshot", captured)
    body = (await client.get(URL, params=PARAMS)).json()
    assert_shared_basis(body)
    assert body["activeAlerts"]["evaluationState"] == "ERROR"
    assert body["snapshot"]["summary"]["loadPercent"] is None


async def test_administrator_can_read_combined_response(client):
    login_response = await client.post(
        "/api/v1/session", json={"username": "admin", "password": "admin1234"}
    )
    assert login_response.status_code == 200
    response = await client.get(URL, params=PARAMS)
    assert response.status_code == 200
    assert_shared_basis(response.json())


async def test_repeated_reads_preserve_history_cooldown_and_tracker(client, test_db_session):
    db = test_db_session
    await tick(client, db, 0.99, 0)
    await login(client)
    tracker_key = ("site-demo", "edge-demo", TRACKER_KIND)
    tracker_before = deepcopy((await db.get(EdgeV2Latest, tracker_key)).payload)
    operation_before = (await db.get(EdgeV2Operation, "pit-demo")).last_alert_at
    records_before = [(row.id, row.read_at) for row in (await db.scalars(select(AlertLog))).all()]
    identities = set()
    for _ in range(3):
        body = (await client.get(URL, params={**PARAMS, "scope": "ALL"})).json()
        assert_shared_basis(body)
        identities.add(body["snapshot"]["snapshotId"])
        assert not db.in_transaction()
    assert len(identities) == 1
    assert (await db.get(EdgeV2Latest, tracker_key)).payload == tracker_before
    assert (await db.get(EdgeV2Operation, "pit-demo")).last_alert_at == operation_before
    assert [
        (row.id, row.read_at) for row in (await db.scalars(select(AlertLog))).all()
    ] == records_before
    assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_error_or_cancellation_releases_read_transaction(
    client, test_db_session, monkeypatch, failure
):
    await login(client)

    async def fail(*args, **kwargs):
        assert test_db_session.in_transaction()
        raise failure()

    monkeypatch.setattr(route, "active_snapshot_alerts", fail)
    with pytest.raises(failure):
        await client.get(URL, params=PARAMS)
    assert not test_db_session.in_transaction()


async def test_combined_api_reads_committed_data_in_independent_session(
    client, test_db_session, test_engine
):
    await tick(client, test_db_session, 0.99, 0)
    app = create_app()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def fresh_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db_session] = fresh_db
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as reader:
        await login(reader)
        response = await reader.get(URL, params=PARAMS)
    assert response.status_code == 200
    assert_shared_basis(response.json())
    assert response.json()["activeAlerts"]["evaluationState"] == "CRITICAL"


def test_openapi_requires_both_parts_and_uses_existing_scope_contract():
    schema = create_app().openapi()
    operation = schema["paths"][URL]["get"]
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    body = schema["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]
    assert set(body["required"]) == {"snapshot", "activeAlerts"}
    scope = next(x["schema"] for x in operation["parameters"] if x["name"] == "scope")
    assert scope["enum"] == ["FILL_OPERATIONS", "ALL"]
