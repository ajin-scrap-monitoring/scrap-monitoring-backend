"""Long synthetic histories preserve gaps, statistics, source changes and bounded reads."""

import json
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, text
from test_load_history_v2 import history_config as history_config
from test_monitoring_v2 import login

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.services.compact_history_queries import compact_aggregate_query
from src.app.services.load_history_v2_service import MAX_POINTS, history_window, load_history
from src.app.services.monitoring_v2_service import monitoring_source

START = datetime(2026, 1, 1, tzinfo=UTC)


def minute(at, *, method="synthetic-v1", source="EDGE_STATE", invalid_last=False):
    values, metadata, methods, sources = {}, {}, {}, {}
    for offset, value in [(1, 10.0), (20, 30.0), (40, None), (59, None if invalid_last else 50.0)]:
        generated = at + timedelta(seconds=offset, microseconds=200000)
        stamp = str(int(generated.timestamp()))
        values[stamp], methods[stamp], sources[stamp] = value, method, source
        last = {
            "generatedAt": generated.isoformat(),
            "observedAt": generated.isoformat(),
            "assessmentId": "synthetic-" + stamp,
            "dataState": "AVAILABLE" if value is not None else "UNAVAILABLE",
            "reasonCodes": [] if value is not None else ["SYNTHETIC_INVALID"],
        }
        metadata[stamp] = last
    return {
        "site_id": "site-demo",
        "edge_id": "edge-demo",
        "bucket_start": at,
        "generated_at": generated,
        "payload": {
            "values": values,
            "metadata": metadata,
            "methods": methods,
            "sources": sources,
            "last": last,
        },
    }


@pytest.mark.parametrize("days,seconds,size", [(30, 3600, 720), (90, 7200, 1080)])
async def test_long_api_range_uses_coarse_buckets_and_never_fills_missing_data_with_zero(
    client, days, seconds, size
):
    await login(client)
    response = await client.get(
        "/api/v2/monitoring/load-history",
        params={
            "pit_id": "pit-demo",
            "from": START.isoformat(),
            "to": (START + timedelta(days=days)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["bucketSeconds"] == seconds and len(data["samples"]) == size
    assert all(
        point["valuePercent"] is None and point["dataState"] == "NO_DATA"
        for point in data["samples"]
    )


@pytest.mark.parametrize("seconds", [60, 300, 900, 3600, 7200, 21600, 86400])
async def test_explicit_interval_query_strings_are_accepted(client, seconds):
    await login(client)
    response = await client.get(
        "/api/v2/monitoring/load-history",
        params={
            "pit_id": "pit-demo",
            "from": START.isoformat(),
            "to": (START + timedelta(hours=1)).isoformat(),
            "bucket_seconds": seconds,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["bucketSeconds"] == seconds


@pytest.mark.parametrize("seconds", [7200, 21600, 86400])
def test_explicit_large_intervals_and_partial_endpoints_stay_bounded(seconds):
    start = START + timedelta(seconds=1, microseconds=1)
    end = start + timedelta(days=90)
    _, _, chosen = history_window(start, end, seconds, end)
    assert chosen == seconds
    with pytest.raises(HTTPException) as error:
        history_window(start, end, 3600, end)
    assert error.value.status_code == 422 and error.value.detail == "HISTORY_TOO_MANY_POINTS"


async def test_complete_and_partial_minutes_merge_without_losing_last_invalid_or_statistics(
    test_db_session,
):
    db = test_db_session
    data = [
        minute(START),
        minute(START + timedelta(minutes=1)),
        minute(START + timedelta(minutes=2), invalid_last=True),
    ]
    await db.execute(insert(EdgeV2Minute), data)
    await db.commit()
    source = monitoring_source("pit-demo")
    # Skip the first 10.0 and retain the last invalid reading instead of an earlier good one.
    result = await load_history(
        db,
        source,
        START + timedelta(seconds=2),
        START + timedelta(minutes=3),
        3600,
        START + timedelta(days=1),
    )
    sample = result.samples[0]
    assert sample.valuePercent is None and not sample.valid
    assert sample.dataState == "UNAVAILABLE" and sample.reasonCodes == ["SYNTHETIC_INVALID"]
    assert sample.validSamples == 7 and sample.invalidSamples == 4
    assert sample.averagePercent == pytest.approx(210 / 7)
    assert sample.minimumPercent == 10 and sample.maximumPercent == 50
    assert sample.generatedAt == START + timedelta(minutes=2, seconds=59.2)


async def test_sql_and_boundary_parts_preserve_mixed_methods_and_simulation_label(test_db_session):
    db = test_db_session
    await db.execute(
        insert(EdgeV2Minute),
        [
            minute(START),
            minute(
                START + timedelta(minutes=1),
                method="SIMULATION_SENSOR_ANGLE_V1:synthetic",
                source="HEIGHT_SECTIONS",
            ),
        ],
    )
    await db.commit()
    result = await load_history(
        db,
        monitoring_source("pit-demo"),
        START + timedelta(seconds=2),
        START + timedelta(minutes=2),
        3600,
        START + timedelta(days=1),
    )
    sample = result.samples[0]
    assert sample.inputSource == "MIXED" and sample.methodVersion is None
    assert sample.simulated and sample.valuePercent is None
    assert (
        sample.averagePercent is None
        and sample.minimumPercent is None
        and sample.maximumPercent is None
    )
    assert sample.validSamples == 0 and sample.invalidSamples == 7
    assert sample.reasonCodes == ["FILL_METHOD_CHANGED_WITHIN_BUCKET"]


async def test_sql_reduction_returns_only_small_bucket_summaries_and_scopes_by_source(
    test_db_session,
):
    db = test_db_session
    rows = [minute(START + timedelta(minutes=index)) for index in range(180)]
    other = minute(START, method="other")
    other["edge_id"] = "other-edge"
    await db.execute(insert(EdgeV2Minute), [*rows, other])
    await db.commit()
    source = monitoring_source("pit-demo")
    summaries = (
        (
            await db.execute(
                compact_aggregate_query(db, source, START, START + timedelta(hours=3), 3600)
            )
        )
        .mappings()
        .all()
    )
    assert len(summaries) == 3
    for summary in summaries:
        assert summary["count"] == 180 and summary["invalid"] == 60
        assert summary["source_count"] == summary["method_count"] == 1
        assert "payload" not in summary and "values" not in summary
    result = await load_history(
        db, source, START, START + timedelta(hours=3), 3600, START + timedelta(days=1)
    )
    assert all(p.valuePercent == 50 and p.averagePercent == 30 for p in result.samples)


async def test_method_case_does_not_invent_a_simulation_label(test_db_session):
    db = test_db_session
    await db.execute(
        insert(EdgeV2Minute), [minute(START, method="simulation_sensor_angle_v1:other")]
    )
    await db.commit()
    result = await load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(minutes=1),
        60,
        START + timedelta(days=1),
    )
    assert result.samples[0].valid and not result.samples[0].simulated


async def test_ninety_days_of_minutes_have_bounded_results_and_indexed_range(
    test_db_session, request
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Dense 90-day query cost is verified against PostgreSQL")
    db = test_db_session
    end = START + timedelta(days=90)
    # Insert in small batches: 129,600 minutes / 518,400 scalar slots, no real data.
    for offset in range(0, 90 * 1440, 1000):
        await db.execute(
            insert(EdgeV2Minute),
            [
                minute(START + timedelta(minutes=i))
                for i in range(offset, min(offset + 1000, 90 * 1440))
            ],
        )
    await db.commit()
    await db.execute(text("ANALYZE edge_v2_minutes"))
    source = monitoring_source("pit-demo")
    query = compact_aggregate_query(db, source, START, end, 7200)
    started = perf_counter()
    result = await load_history(db, source, START, end, None, end)
    elapsed = perf_counter() - started
    assert len(result.samples) == 1080 <= MAX_POINTS
    assert sum(p.validSamples for p in result.samples) == 90 * 1440 * 3
    assert sum(p.invalidSamples for p in result.samples) == 90 * 1440
    assert all(
        p.averagePercent == 30 and p.minimumPercent == 10 and p.maximumPercent == 50
        for p in result.samples
    )
    # Check an independently restricted window uses the existing source/time primary key.
    bounded = compact_aggregate_query(db, source, end - timedelta(days=1), end, 3600)
    compiled = bounded.compile(
        dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )
    plan = (await db.execute(text("EXPLAIN (FORMAT JSON) " + str(compiled)))).scalar_one()
    assert "Index" in json.dumps(plan) and "edge_v2_minutes_pkey" in json.dumps(plan)
    assert elapsed < 30, f"90-day SQL aggregation exceeded test budget: {elapsed:.2f}s"
    print(
        "\nSynthetic90DayHistory minutes=129600 slots=518400 returned=1080 "
        f"elapsedSeconds={elapsed:.3f}"
    )
    summaries = (await db.execute(query)).mappings().all()
    assert len(summaries) == 1080 and all("payload" not in row for row in summaries)
