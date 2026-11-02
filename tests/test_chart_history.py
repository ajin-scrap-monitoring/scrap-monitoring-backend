"""Graph projections preserve all time, numerical and quality semantics."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import event, insert
from test_load_history_v2 import BASE, START
from test_load_history_v2 import history_config as history_config
from test_long_load_history import minute
from test_monitoring_v2 import SOURCE, assessment, login, send

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.schemas.load_history_v2 import ChartHistoryV2, HistorySampleV2, LoadHistoryV2
from src.app.services.compact_history_summary import build_minute_summary

OMITTED = {"assessmentId", "observationId", "methodVersion"}
PARAMS = {
    "pit_id": "pit-demo",
    "from": START.isoformat(),
    "to": (START + timedelta(minutes=2)).isoformat(),
    "bucket_seconds": 60,
}


def assert_projection(full, chart):
    assert chart["view"] == "chart"
    assert {k: v for k, v in chart.items() if k not in {"view", "samples"}} == {
        k: v for k, v in full.items() if k != "samples"
    }
    assert len(chart["samples"]) == len(full["samples"])
    for original, projected in zip(full["samples"], chart["samples"], strict=True):
        assert not OMITTED.intersection(projected)
        assert projected == {k: v for k, v in original.items() if k not in OMITTED}


@pytest.mark.parametrize("mode", ["raw", "compact", "mixed", "partial", "invalid"])
async def test_chart_matches_full_with_same_queries(client, test_db_session, test_engine, mode):
    params = dict(PARAMS)
    if mode == "raw":
        await send(client, assessment("chart-zero", 0.0, START + timedelta(seconds=1)))
    else:
        rows = [
            minute(START + timedelta(minutes=n), invalid_last=mode == "invalid") for n in range(2)
        ]
        if mode == "mixed":
            row = rows[0]
            row["payload"]["methods"][next(iter(row["payload"]["values"]))] = "other-method"
        for row in rows:
            row["history_summary"] = build_minute_summary(
                row["bucket_start"], row["generated_at"], row["payload"]
            )
        await test_db_session.execute(insert(EdgeV2Minute), rows)
        await test_db_session.commit()
        if mode == "partial":
            params["from"] = (START + timedelta(seconds=2.3)).isoformat()
            params["to"] = (START + timedelta(minutes=1, seconds=20.1)).isoformat()
    await login(client)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        full = await client.get(BASE, params=params)
        full_statements = list(statements)
        statements.clear()
        chart = await client.get(BASE, params={**params, "view": "chart"})
        assert statements == full_statements
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert full.status_code == chart.status_code == 200
    assert full.headers["cache-control"] == chart.headers["cache-control"] == "no-store"
    assert_projection(full.json(), chart.json())
    if mode == "raw":
        assert chart.json()["samples"][0]["valuePercent"] == 0
        assert chart.json()["samples"][0]["valid"] is True
        assert chart.json()["samples"][1]["valuePercent"] is None


@pytest.mark.parametrize(
    "state", ["AVAILABLE", "LIMITED", "UNAVAILABLE", "STALE", "UNKNOWN", "NO_DATA"]
)
def test_chart_keeps_quality_simulation_statistics_and_display_precision(state):
    numeric = state in {"AVAILABLE", "LIMITED"}
    sample = HistorySampleV2(
        bucketStartAt=START,
        bucketEndAt=START + timedelta(minutes=1),
        generatedAt=START + timedelta(seconds=50),
        observedAt=START + timedelta(seconds=49),
        assessmentId="synthetic-assessment",
        observationId="synthetic-observation",
        methodVersion="synthetic-method",
        simulated=True,
        inputSource="MIXED" if state == "UNKNOWN" else "HEIGHT_SECTIONS",
        valuePercent=42.56789 if numeric else None,
        valid=numeric,
        dataState=state,
        reasonCodes=["SYNTHETIC_QUALITY"],
        averagePercent=40.12345 if numeric else None,
        minimumPercent=0.0 if numeric else None,
        maximumPercent=80.6789 if numeric else None,
        validSamples=3 if numeric else 0,
        invalidSamples=1 if numeric else 4,
        valuePolicy="MEDIAN_MINUTE",
    )
    full = LoadHistoryV2(
        pitId="pit-demo",
        source=SOURCE,
        from_time=START,
        to_time=START + timedelta(minutes=1),
        bucketSeconds=60,
        samples=[sample],
    )
    before = full.model_dump_json(by_alias=True)
    chart = ChartHistoryV2.from_history(full)
    assert_projection(json.loads(before), json.loads(chart.model_dump_json(by_alias=True)))
    assert full.model_dump_json(by_alias=True) == before


def test_dense_chart_reduces_repeated_identifier_bytes():
    full = LoadHistoryV2(
        pitId="pit-demo",
        source=SOURCE,
        from_time=START,
        to_time=START + timedelta(days=1),
        bucketSeconds=60,
        samples=[
            HistorySampleV2(
                bucketStartAt=START + timedelta(minutes=n),
                bucketEndAt=START + timedelta(minutes=n + 1),
                generatedAt=START + timedelta(minutes=n, seconds=59),
                observedAt=START + timedelta(minutes=n, seconds=58),
                assessmentId="synthetic-assessment-" + str(n) + "x" * 80,
                observationId="synthetic-observation-" + str(n) + "y" * 80,
                methodVersion="synthetic-method-" + "z" * 100,
                valuePercent=0.0 if n % 2 else None,
                valid=bool(n % 2),
                dataState="AVAILABLE" if n % 2 else "NO_DATA",
                reasonCodes=[] if n % 2 else ["STATE_NOT_RECEIVED"],
            )
            for n in range(1440)
        ],
    )
    original = full.model_dump_json(by_alias=True)
    chart = ChartHistoryV2.from_history(full).model_dump_json(by_alias=True)
    assert len(chart.encode()) < len(original.encode()) * 0.7
    assert_projection(json.loads(original), json.loads(chart))


async def test_old_bucket_can_change_after_delayed_data(client, test_db_session):
    await send(client, assessment("old-first", 0.2, START + timedelta(seconds=1)))
    await login(client)
    first = await client.get(BASE, params={**PARAMS, "view": "chart"})
    assert first.json()["samples"][0]["valuePercent"] == 20
    await send(client, assessment("old-late", 0.8, START + timedelta(seconds=40)))
    later = await client.get(BASE, params={**PARAMS, "view": "chart"})
    full = await client.get(BASE, params=PARAMS)
    assert later.json()["samples"][0]["valuePercent"] == 80
    assert_projection(full.json(), later.json())


async def test_default_full_auth_validation_and_logout(client):
    assert (await client.get(BASE, params={**PARAMS, "view": "chart"})).status_code == 401
    await login(client)
    full = await client.get(BASE, params=PARAMS)
    explicit = await client.get(BASE, params={**PARAMS, "view": "full"})
    assert full.status_code == explicit.status_code == 200 and full.json() == explicit.json()
    assert "view" not in full.json()
    assert (await client.get(BASE, params={**PARAMS, "view": "bad"})).status_code == 422
    assert (
        await client.get(BASE, params={**PARAMS, "view": "chart", "pit_id": "other"})
    ).status_code == 404
    session = await client.get("/api/v1/session")
    csrf = session.json()["csrfToken"]
    assert (
        await client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf})
    ).status_code == 204
    assert (await client.get(BASE, params={**PARAMS, "view": "chart"})).status_code == 401


async def test_chart_openapi_omits_ids_but_keeps_times_and_quality(client):
    schema = (await client.get("/openapi.json")).json()
    view = next(p for p in schema["paths"][BASE]["get"]["parameters"] if p["name"] == "view")
    assert view["schema"]["default"] == "full"
    properties = schema["components"]["schemas"]["ChartHistorySampleV2"]["properties"]
    assert not OMITTED.intersection(properties)
    assert {
        "generatedAt",
        "observedAt",
        "valid",
        "reasonCodes",
        "inputSource",
        "simulated",
    } <= properties.keys()
    assert OMITTED <= schema["components"]["schemas"]["HistorySampleV2"]["properties"].keys()
