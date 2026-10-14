"""Synthetic historical STATE queries: bounds, nulls, SQL reduction and scoped authentication."""

import json
from datetime import UTC, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from test_monitoring_v2 import DEVICE, NOW, SOURCE, TOKEN, assessment, login, send

from src.app.core.config import get_settings
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.services.load_history_v2_service import history_window, load_history
from src.app.services.monitoring_v2_service import monitoring_source

BASE = "/api/v2/monitoring/load-history"
START = NOW.replace(second=0, microsecond=0) - timedelta(minutes=10)


@pytest.fixture(autouse=True)
def history_config(monkeypatch):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def history(db, start=START, end=None, interval=60):
    return await load_history(
        db, monitoring_source("pit-demo"), start, end or start + timedelta(minutes=3), interval, NOW
    )


async def test_authentication_and_explicit_source(client):
    r = await client.get(
        BASE, params={"pit_id": "pit-demo"}, headers={"Authorization": "Bearer " + TOKEN}
    )
    assert r.status_code == 401
    await login(client)
    r = await client.get(BASE, params={"pit_id": "pit-other"})
    assert r.status_code == 404
    r = await client.get(BASE, params={"pit_id": "pit-demo"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["schemaVersion"] == "2" and body["bucketSeconds"] == 60
    assert 1440 <= len(body["samples"]) <= 1441
    assert all(p["valuePercent"] is None and p["dataState"] == "NO_DATA" for p in body["samples"])
    assert TOKEN not in r.text


async def test_latest_state_not_average_missing_minute_and_real_zero(client, test_db_session):
    await send(client, assessment("first", 0.8, START + timedelta(seconds=1)))
    await send(client, assessment("latest", 0.4, START + timedelta(seconds=50)))
    await send(client, assessment("real-zero", 0.0, START + timedelta(minutes=2, seconds=5)))
    result = await history(test_db_session)
    assert [p.valuePercent for p in result.samples] == [40.0, None, 0.0]
    assert [p.valid for p in result.samples] == [True, False, True]
    assert result.samples[0].assessmentId == "latest"
    assert result.samples[1].reasonCodes == ["STATE_NOT_RECEIVED"]
    assert result.selectionPolicy == "LATEST_STATE_PER_BUCKET"
    # Current status is absent. This cannot erase historical source judgments.
    for model in (ScrapMetric, AlertLog, CollectionLog, MediaClipJob):
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_delayed_old_state_replay_new_envelope_and_event_do_not_replace(
    client, test_db_session
):
    latest = assessment("latest", 0.8, START + timedelta(seconds=40))
    await send(client, latest)
    await send(client, assessment("older", 0.1, START + timedelta(seconds=20)))
    await send(client, latest)
    duplicate = {**latest, "message_id": "different-envelope"}
    await send(client, duplicate)
    event_payload = assessment("event", 0.99, START + timedelta(seconds=55))
    event_payload.update(
        assessment_type="EVENT", revision=4, event={"kind": "COLLECTION", "stage": "CONFIRMED"}
    )
    await send(client, event_payload)
    result = await history(test_db_session)
    assert result.samples[0].valuePercent == 80
    assert result.samples[0].assessmentId == "latest"
    assert len(result.samples) == 3


async def test_latest_unavailable_state_clears_prior_valid_value_in_bucket(client, test_db_session):
    await send(client, assessment("valid", 0.9, START + timedelta(seconds=1)))
    unavailable = assessment("unavailable", when=START + timedelta(seconds=59))
    unavailable.update(
        fill_estimate=None,
        fill_state="UNAVAILABLE",
        decision_state="UNAVAILABLE",
        reason_codes=["FILL_NOT_VALIDATED"],
    )
    unavailable["capabilities"]["fill"] = "NOT_VALIDATED"
    await send(client, unavailable)
    point = (await history(test_db_session)).samples[0]
    assert point.valuePercent is None and not point.valid
    assert point.dataState == "UNAVAILABLE"
    assert point.reasonCodes == ["FILL_NOT_VALIDATED"]


@pytest.mark.parametrize(
    "case,state,reason",
    [
        ("stale", "STALE", "ASSESSMENT_STALE_AT_GENERATION"),
        ("future_observation", "UNKNOWN", "ASSESSMENT_CLOCK_SKEW"),
        ("no_observation", "UNKNOWN", "OBSERVATION_TIME_UNAVAILABLE"),
        ("limited", "LIMITED", "PARTIAL_INPUT"),
    ],
)
async def test_historical_validity_is_relative_to_generation(
    client, test_db_session, case, state, reason
):
    a = assessment("historical", 0.7, START + timedelta(seconds=20))
    if case == "stale":
        a["observed_from"] = a["observed_to"] = (START - timedelta(seconds=10)).isoformat()
    elif case == "future_observation":
        a["observed_from"] = a["observed_to"] = (START + timedelta(seconds=21)).isoformat()
    elif case == "no_observation":
        a.update(observed_from=None, observed_to=None)
    elif case == "limited":
        a.update(decision_state="LIMITED", reason_codes=["PARTIAL_INPUT"])
    await send(client, a)
    point = (await history(test_db_session)).samples[0]
    assert point.dataState == state and reason in point.reasonCodes
    assert point.valuePercent == (70 if case == "limited" else None)


async def test_old_report_received_today_is_placed_at_original_generation(client, test_db_session):
    old = NOW - timedelta(hours=12)
    await send(client, assessment("old-but-valid", 0.6, old))
    result = await history(test_db_session, old - timedelta(seconds=1), old + timedelta(seconds=1))
    points = [p for p in result.samples if p.valuePercent is not None]
    assert len(points) == 1 and points[0].valuePercent == 60
    assert points[0].generatedAt == old
    assert points[0].observedAt == old


async def test_half_open_microsecond_range_and_partial_buckets(client, test_db_session):
    start = START + timedelta(seconds=10, microseconds=500000)
    end = START + timedelta(minutes=1, seconds=10, microseconds=500000)
    for name, when, ratio in (
        ("before", start - timedelta(microseconds=1), 0.1),
        ("at-start", start, 0.2),
        ("just-before-end", end - timedelta(microseconds=1), 0.3),
        ("at-end", end, 0.4),
    ):
        await send(client, assessment(name, ratio, when))
    r = await history(test_db_session, start, end)
    assert [p.valuePercent for p in r.samples] == [20, 30]
    assert r.samples[0].bucketStartAt == start
    assert r.samples[-1].bucketEndAt == end


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-01-01T00:00:00"},
        {"to": "2026-01-01T00:00:00"},
        {"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-09T00:00:00Z"},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-08T00:00:00Z", "bucket_seconds": 60},
        {"bucket_seconds": 1},
        {"to": "9999-12-31T23:59:59Z"},
        {"to": "0001-01-01T00:00:00Z"},
        {"from": "0001-01-01T00:00:00+09:00"},
    ],
)
async def test_invalid_query_is_rejected(client, params):
    await login(client)
    r = await client.get(BASE, params={"pit_id": "pit-demo", **params})
    assert r.status_code == 422


def test_adaptive_intervals_and_timezone_normalization():
    _, _, interval = history_window(NOW - timedelta(days=7), NOW, None, NOW)
    assert interval == 900
    korea = timezone(timedelta(hours=9))
    start, end, interval = history_window(
        (NOW - timedelta(hours=24)).astimezone(korea), NOW.astimezone(korea), None, NOW
    )
    assert start.tzinfo == UTC and end == NOW and interval == 60


async def test_scope_isolation_even_when_assessment_id_matches(
    client, test_db_session, monkeypatch
):
    other = {**DEVICE, "edge_id": "edge-other", "token": "synthetic-other-history-token"}
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE, other]))
    get_settings.cache_clear()
    a = assessment("same-id", 0.99, START + timedelta(seconds=50))
    a["edge_id"] = "edge-other"
    r = await client.post(
        "/api/edge/v2/assessments",
        json=a,
        headers={
            "Authorization": "Bearer synthetic-other-history-token",
            "Idempotency-Key": a["message_id"],
        },
    )
    assert r.status_code == 201
    await send(client, assessment("same-id", 0.3, START + timedelta(seconds=1)))
    assert (await history(test_db_session)).samples[0].valuePercent == 30


async def test_one_bounded_sql_select_uses_time_range_and_rank(
    client, test_db_session, test_engine
):
    for second in range(120):
        await send(
            client, assessment(f"s-{second}", second / 120, START + timedelta(seconds=second))
        )
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await history(test_db_session, end=START + timedelta(minutes=2))
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "row_number() OVER" in statements[0]
    assert "generated_at >=" in statements[0] and "generated_at <" in statements[0]
    assert len(result.samples) == 2
    assert [p.assessmentId for p in result.samples] == ["s-59", "s-119"]


async def test_v1_history_is_unchanged_and_v2_has_no_threshold_history(client):
    await send(client, assessment("v2-only", 0.6, START))
    r = await client.get("/api/v1/history/load", params={"pit_id": "pit-demo"})
    assert r.status_code == 200 and r.json()["samples"] == []
    await login(client)
    r = await client.get(
        BASE,
        params={
            "pit_id": "pit-demo",
            "from": START.isoformat(),
            "to": (START + timedelta(minutes=1)).isoformat(),
        },
    )
    assert r.status_code == 200 and r.json()["samples"][0]["valuePercent"] == 60
    assert "collectionThresholds" not in r.json()
