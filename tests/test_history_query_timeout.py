"""Transaction-local query budgets and conservative error responses for history."""

from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_load_history_v2 import history_config as history_config
from test_long_load_history import START, minute
from test_monitoring_v2 import login

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.services import load_history_v2_service as service
from src.app.services.compact_history_stream import compact_stream_groups
from src.app.services.monitoring_v2_service import monitoring_source


@pytest.mark.parametrize(
    "mode", ["cached", "legacy", "mixed", "late", "partial", "zero", "invalid"]
)
async def test_streaming_preserves_the_existing_compact_results(test_db_session, mode):
    db = test_db_session
    rows = [minute(START + timedelta(minutes=i)) for i in range(4)]
    for i, row in enumerate(rows):
        payload = row["payload"]
        if mode == "zero":
            payload["values"] = {
                k: 0.0 if v is not None else None for k, v in payload["values"].items()
            }
        if mode == "invalid":
            payload["values"] = dict.fromkeys(payload["values"])
        if mode == "mixed":
            payload["methods"] = {k: str(i) for k in payload["values"]}
        if mode == "late":
            earliest = min(payload["values"], key=int)
            payload["last"] = payload["metadata"][earliest]
            row["generated_at"] = START + timedelta(minutes=i, seconds=1.2)
        if mode != "legacy":
            values = [v for v in payload["values"].values() if v is not None]
            payload.update(
                valid_count=len(values),
                invalid_count=4 - len(values),
                sum=sum(values),
                minimum=min(values, default=None),
                maximum=max(values, default=None),
            )
    await db.execute(insert(EdgeV2Minute), rows)
    await db.commit()
    start = START + timedelta(seconds=2.3) if mode == "partial" else START
    end = (
        START + timedelta(minutes=3, seconds=20.1)
        if mode == "partial"
        else START + timedelta(minutes=4)
    )
    source = monitoring_source("pit-demo")
    streamed = service._compact_result(
        await compact_stream_groups(db, source, start, end, 3600), start, end
    )
    # The SQL path is an independent comparison on SQLite. PostgreSQL uses the stream path.
    if db.get_bind().dialect.name == "sqlite":
        assert streamed == await service.compact_samples(db, source, start, end, 3600)
    assert len(streamed) == 1
    point = next(iter(streamed.values()))[1]
    if mode == "late":
        assert point["generatedAt"] == START + timedelta(minutes=3, seconds=59.2)
    if mode == "mixed":
        assert point["inputSource"] == "MIXED" and point["valuePercent"] is None
    if mode == "zero":
        assert point["valid"] and point["valuePercent"] == 0


async def test_stream_limits_provenance_sets_without_losing_mixed_state(test_db_session):
    db = test_db_session
    rows = [minute(START + timedelta(minutes=i), method=f"method-{i}") for i in range(90)]
    await db.execute(insert(EdgeV2Minute), rows)
    await db.commit()
    groups = await compact_stream_groups(
        db, monitoring_source("pit-demo"), START, START + timedelta(hours=2), 7200
    )
    assert len(groups) == 1
    group = next(iter(groups.values()))
    assert len(group["methods"]) == 2 and group["count"] == 270


async def test_dense_minutes_with_full_metadata_finish_with_bounded_buckets(
    test_db_session, request
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Dense metadata throughput is verified against PostgreSQL")
    db = test_db_session
    minutes = 7 * 1440
    for batch in range(0, minutes, 256):
        rows = []
        for index in range(batch, min(batch + 256, minutes)):
            at = START + timedelta(minutes=index)
            values, metadata, sources, methods = {}, {}, {}, {}
            for second in range(60):
                generated = at + timedelta(seconds=second, microseconds=200000)
                stamp = str(int(generated.timestamp()))
                value = None if second % 10 == 0 else 50.0
                values[stamp], sources[stamp], methods[stamp] = (
                    value,
                    "EDGE_STATE",
                    "synthetic-wide",
                )
                last = {
                    "generatedAt": generated.isoformat(),
                    "observedAt": generated.isoformat(),
                    "assessmentId": "synthetic-" + "x" * 80 + stamp,
                    "observationId": "synthetic-" + "y" * 80 + stamp,
                    "dataState": "AVAILABLE" if value is not None else "UNAVAILABLE",
                    "reasonCodes": [] if value is not None else ["SYNTHETIC_INVALID"],
                }
                metadata[stamp] = last
            rows.append(
                {
                    "site_id": "site-demo",
                    "edge_id": "edge-demo",
                    "bucket_start": at,
                    "generated_at": generated,
                    "payload": {
                        "values": values,
                        "metadata": metadata,
                        "sources": sources,
                        "methods": methods,
                        "last": last,
                        "valid_count": 54,
                        "invalid_count": 6,
                        "sum": 2700.0,
                        "minimum": 50.0,
                        "maximum": 50.0,
                    },
                }
            )
        await db.execute(insert(EdgeV2Minute), rows)
    await db.commit()
    started = perf_counter()
    result = await service.load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(days=7),
        3600,
        START + timedelta(days=8),
    )
    elapsed = perf_counter() - started
    assert len(result.samples) == 168
    assert sum(p.validSamples for p in result.samples) == minutes * 54
    assert sum(p.invalidSamples for p in result.samples) == minutes * 6
    assert all(
        p.valuePercent == p.averagePercent == p.minimumPercent == p.maximumPercent == 50
        for p in result.samples
    )
    print(
        f"\nDenseCompactHistory minutes={minutes} slots={minutes * 60} "
        f"points=168 seconds={elapsed:.3f}"
    )


async def test_three_methods_still_report_mixed_history(test_db_session):
    db = test_db_session
    await db.execute(
        insert(EdgeV2Minute),
        [minute(START + timedelta(minutes=i), method=str(i)) for i in range(3)],
    )
    await db.commit()
    result = await service.load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(hours=1),
        3600,
        START + timedelta(days=1),
    )
    point = result.samples[0]
    assert point.inputSource == "MIXED" and point.methodVersion is None
    assert point.valuePercent is None and point.invalidSamples == 12 and point.validSamples == 0


async def test_postgresql_settings_are_local_to_the_history_transaction(
    test_db_session, test_engine, request
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Requires PostgreSQL transaction-local settings")
    db = test_db_session
    original_jit = await db.scalar(text("SHOW jit"))
    original_timeout = await db.scalar(text("SHOW statement_timeout"))
    await service.load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(hours=1),
        3600,
        START + timedelta(days=1),
    )
    assert await db.scalar(text("SHOW jit")) == "off"
    assert await db.scalar(text("SHOW statement_timeout")) == "20s"
    async with async_sessionmaker(test_engine)() as other:
        assert await other.scalar(text("SHOW jit")) == original_jit
        assert await other.scalar(text("SHOW statement_timeout")) == original_timeout
    await db.commit()
    assert await db.scalar(text("SHOW jit")) == original_jit
    assert await db.scalar(text("SHOW statement_timeout")) == original_timeout


async def test_postgresql_cancelled_history_returns_503_without_empty_samples(
    client, test_db_session, request, monkeypatch
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Requires PostgreSQL statement cancellation")
    await login(client)

    async def delayed(db, *args):
        await db.execute(text("SELECT pg_sleep(0.05)"))
        pytest.fail("Query should have timed out")

    monkeypatch.setattr(service, "HISTORY_QUERY_TIMEOUT_MILLISECONDS", 5)
    monkeypatch.setattr(service, "_read_history", delayed)
    response = await client.get("/api/v2/monitoring/load-history", params={"pit_id": "pit-demo"})
    assert response.status_code == 503
    assert response.json() == {"detail": "HISTORY_QUERY_TIMEOUT"}
    await test_db_session.rollback()
    assert await test_db_session.scalar(text("SHOW statement_timeout")) != "5ms"
