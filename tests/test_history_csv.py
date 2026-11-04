"""CSV downloads retain JSON evidence, remain scoped/read-only and fail without partial files."""

import asyncio
import csv
import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_chart_history import PARAMS
from test_load_history_v2 import START
from test_load_history_v2 import history_config as history_config
from test_long_load_history import minute
from test_monitoring_v2 import assessment, login, send

from src.app.models.edge_v2_projection import EdgeV2Minute, EdgeV2Transition
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.services import history_csv

LOAD = "/api/v2/monitoring/load-history.csv"
EVENTS = "/api/v1/events.csv"


def rows(response):
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith('attachment; filename="')
    result = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(result) == int(response.headers["x-csv-row-count"])
    return result


@pytest.mark.parametrize("mode", ["raw", "compact", "mixed", "invalid"])
async def test_load_csv_matches_json_without_writes(client, test_db_session, test_engine, mode):
    if mode == "raw":
        await send(client, assessment("csv-zero", 0.0, START + timedelta(seconds=1)))
    else:
        await test_db_session.execute(
            insert(EdgeV2Minute),
            [minute(START, invalid_last=mode == "invalid")],
        )
        await test_db_session.commit()
        if mode == "mixed":
            await send(client, assessment("csv-later", 0.76567, START + timedelta(seconds=59)))
    await login(client)
    original = (await client.get(LOAD[:-4], params=PARAMS)).json()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await client.get(LOAD, params=PARAMS)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    exported = rows(response)
    assert len(exported) == len(original["samples"])
    for actual, expected in zip(exported, original["samples"], strict=True):
        assert actual["bucketSeconds"] == str(original["bucketSeconds"])
        assert actual["from"] == original["from"] and actual["to"] == original["to"]
        assert actual["pitId"] == "pit-demo"
        for key, value in expected.items():
            assert actual[key] == history_csv.cell(value), key
    if mode == "raw":
        assert exported[0]["valuePercent"] == "0.0"
        assert exported[1]["valuePercent"] == ""
        assert exported[1]["valid"] == "false"
        assert exported[1]["dataState"] == "NO_DATA"
    assert not any(
        s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements
    )
    assert not test_db_session.in_transaction()


async def seed_events(db):
    db.add_all(
        [
            CollectionLog(
                pit_id="pit-demo",
                completed_at=START + timedelta(seconds=1),
                residual_height_cm=0,
                operator_name="작업자",
                notes='한글, "인용"\n다음 줄',
            ),
            AlertLog(
                pit_id="pit-demo",
                triggered_at=START + timedelta(seconds=2),
                fill_ratio_percent=90,
                message='=HYPERLINK("https://example.invalid")',
            ),
            ScrapMetric(
                measurement_id="csv-invalid",
                pit_id="pit-demo",
                measured_at=START + timedelta(seconds=3),
                is_valid=False,
                state="ERROR",
            ),
            CollectionLog(
                pit_id="other",
                completed_at=START,
                residual_height_cm=0,
                operator_name="제외",
                notes="다른 피트",
            ),
        ]
    )
    await db.commit()


@pytest.mark.parametrize("category", ["all", "collection", "alert", "error"])
async def test_event_csv_matches_filtered_json_and_preserves_source(
    client, test_db_session, category
):
    await seed_events(test_db_session)
    await login(client)
    params = {k: v for k, v in PARAMS.items() if k != "bucket_seconds"}
    params.update(category=category, collection_scope="all")
    expected = (await client.get("/api/v1/events", params=params)).json()["items"]
    actual = rows(await client.get(EVENTS, params=params))
    assert len(actual) == len(expected)
    assert {x["category"] for x in actual} <= {"collection", "alert", "error"}
    for row, item in zip(actual, expected, strict=True):
        for key, value in item.items():
            assert row[key] == history_csv.cell(value)
        assert row["collectionScope"] == "all" and row["pitId"] == "pit-demo"
    assert not test_db_session.in_transaction()
    if category == "all":
        assert actual[1]["detail"].startswith("'=HYPERLINK")
        assert actual[2]["detail"] == '작업자: 한글, "인용"\n다음 줄'
        stored = await test_db_session.scalar(select(AlertLog).where(AlertLog.pit_id == "pit-demo"))
        assert stored.message.startswith("=HYPERLINK")


async def test_error_recovery_evidence_is_not_flattened(client, test_db_session):
    test_db_session.add(
        EdgeV2Transition(
            site_id="site-demo",
            edge_id="edge-demo",
            kind="MEASUREMENT_VALIDITY_CHANGED",
            occurred_at=START + timedelta(seconds=5),
            payload={
                "pitId": "pit-demo",
                "previousValid": False,
                "currentValid": True,
                "simulated": True,
                "reasonCodes": ["SYNTHETIC_RECOVERY"],
            },
        )
    )
    await test_db_session.commit()
    await login(client)
    result = rows(await client.get(EVENTS, params={**PARAMS, "category": "error"}))
    assert len(result) == 1
    assert result[0]["stateAtOccurrence"] == "RECOVERED"
    assert result[0]["statusBasis"] == "AT_OCCURRENCE"
    assert result[0]["simulated"] == "true"
    assert json.loads(result[0]["reasonCodes"]) == ["SYNTHETIC_RECOVERY"]
    assert result[0]["resolvedAt"] == history_csv.stamp(START + timedelta(seconds=5))


@pytest.mark.parametrize("url", [LOAD, EVENTS])
async def test_authentication_viewer_access_logout_and_safe_errors(client, url):
    response = await client.get(url, params=PARAMS)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    login_response = await client.post(
        "/api/v1/session", json={"username": "operator", "password": "operator1234"}
    )
    assert login_response.status_code == 200
    rows(await client.get(url, params=PARAMS))
    assert (await client.get(url, params={**PARAMS, "pit_id": "other"})).status_code == 404
    bad = await client.get(url, params={**PARAMS, "from": "invalid-private-query"})
    assert bad.status_code == 422 and bad.headers["cache-control"] == "no-store"
    assert "invalid-private-query" not in bad.text
    assert "content-disposition" not in bad.headers
    await client.delete(
        "/api/v1/session", headers={"X-CSRF-Token": login_response.json()["csrfToken"]}
    )
    assert (await client.get(url, params=PARAMS)).status_code == 401


@pytest.mark.parametrize("url", [LOAD, EVENTS])
@pytest.mark.parametrize(
    "change",
    [
        {"from": START.replace(tzinfo=None).isoformat()},
        {"to": START.isoformat()},
        {"from": (START - timedelta(days=91)).isoformat()},
    ],
)
async def test_csv_rejects_invalid_time_windows(client, url, change):
    await login(client)
    response = await client.get(url, params=PARAMS | change)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert "content-disposition" not in response.headers


async def test_empty_event_csv_contains_header_and_no_invented_rows(client):
    await login(client)
    response = await client.get(EVENTS, params=PARAMS)
    assert rows(response) == []
    assert '"occurredAt"' in response.content.decode("utf-8-sig")


async def test_limits_reject_instead_of_returning_partial_files(
    client, test_db_session, monkeypatch
):
    await seed_events(test_db_session)
    await login(client)
    monkeypatch.setattr(history_csv, "MAX_EVENT_CANDIDATES", 2)
    result = await client.get(EVENTS, params=PARAMS)
    assert result.status_code == 422 and result.json()["detail"] == "CSV_EVENT_LIMIT_EXCEEDED"
    assert not test_db_session.in_transaction()
    monkeypatch.setattr(history_csv, "MAX_CSV_BYTES", 5)
    result = await client.get(LOAD, params=PARAMS)
    assert result.status_code == 422 and result.json()["detail"] == "CSV_SIZE_LIMIT_EXCEEDED"
    assert "content-disposition" not in result.headers


@pytest.mark.parametrize(
    "value",
    [
        "=1+2",
        "+1",
        "-1",
        "@SUM(A1)",
        "\tdata",
        "\rdata",
        "\ndata",
        "   =1",
        "\ufeff=1",
        "＝1",
        "＋1",
        "－1",
        "＠SUM(A1)",
        '=1,"\n=2',
    ],
)
async def test_formula_text_is_one_escaped_cell(value):
    body = await history_csv.encode_csv(["text"], [{"text": value}])
    parsed = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert parsed == [["text"], ["'" + value]]


async def test_timeout_and_storage_failure_are_json_without_sensitive_sql(client, monkeypatch):
    await login(client)

    async def slow(*args):
        await asyncio.sleep(1)

    monkeypatch.setattr(history_csv, "QUERY_SECONDS", 0.05)
    monkeypatch.setattr(history_csv, "load_history", slow)
    response = await client.get(LOAD, params=PARAMS)
    assert response.status_code == 503 and response.json()["detail"] == "CSV_QUERY_TIMEOUT"
    assert "content-disposition" not in response.headers

    async def broken(*args):
        raise SQLAlchemyError("synthetic-private-db-address")

    monkeypatch.setattr(history_csv, "load_history", broken)
    response = await client.get(LOAD, params=PARAMS)
    assert response.status_code == 503 and "synthetic-private-db-address" not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_cancel_releases_export_transaction(test_db_session):
    ready = asyncio.Event()

    async def waiting():
        async with history_csv.export_transaction(test_db_session):
            await test_db_session.execute(text("SELECT 1"))
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(waiting())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not test_db_session.in_transaction()
    assert await test_db_session.scalar(text("SELECT 1")) == 1


async def test_postgresql_uses_one_read_only_snapshot(test_db_session):
    if test_db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL transaction settings")
    async with history_csv.export_transaction(test_db_session):
        assert await test_db_session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await test_db_session.scalar(text("SHOW transaction_read_only")) == "on"
    assert not test_db_session.in_transaction()


async def test_csv_openapi_documents_downloads_and_filters(client):
    schema = (await client.get("/openapi.json")).json()
    for path in (LOAD, EVENTS):
        operation = schema["paths"][path]["get"]
        assert "text/csv" in operation["responses"]["200"]["content"]
        assert {"pit_id", "from", "to"} <= {p["name"] for p in operation["parameters"]}


async def test_old_collection_scope_is_explicit_and_not_human_confirmation(client, test_db_session):
    test_db_session.add(
        EdgeV2Transition(
            site_id="site-demo",
            edge_id="edge-demo",
            kind="COLLECTION_SETTLED",
            occurred_at=START + timedelta(seconds=5),
            payload={
                "pitId": "pit-demo",
                "scopeToken": "synthetic-previous-scope",
                "detectionId": "synthetic-detection",
                "simulated": True,
            },
        )
    )
    await test_db_session.commit()
    await login(client)
    assert rows(await client.get(EVENTS, params={**PARAMS, "category": "collection"})) == []
    result = rows(
        await client.get(
            EVENTS,
            params={
                **PARAMS,
                "category": "collection",
                "collection_scope": "all",
            },
        )
    )
    assert len(result) == 1
    assert result[0]["confirmedCompletion"] == "false"
    assert result[0]["collectionSource"] == "HEIGHT_SETTLED"
    assert result[0]["simulated"] == "true"
    assert json.loads(result[0]["collectionCriteria"])["scopeToken"] == "synthetic-previous-scope"


async def test_link_input_limit_is_checked_before_existing_assembler(
    client, test_db_session, monkeypatch
):
    for number in range(3):
        test_db_session.add(
            MediaClipJob(
                event_id=f"synthetic-{number}",
                camera_id="synthetic-camera",
                pit_id="pit-demo",
                category="collection",
                occurred_at=START,
                clip_id=f"00000000-0000-0000-0000-{number:012d}",
            )
        )
    await test_db_session.commit()
    await login(client)
    monkeypatch.setattr(history_csv, "MAX_EVENT_CANDIDATES", 2)
    response = await client.get(EVENTS, params=PARAMS)
    assert (
        response.status_code == 422 and response.json()["detail"] == "CSV_REFERENCE_LIMIT_EXCEEDED"
    )
    assert not test_db_session.in_transaction()


async def test_postgresql_snapshot_survives_concurrent_insert(test_engine, test_db_session):
    if test_db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL repeatable read")
    count = select(func.count()).select_from(CollectionLog)
    async with history_csv.export_transaction(test_db_session):
        before = await test_db_session.scalar(count)
        async with async_sessionmaker(test_engine)() as writer:
            writer.add(
                CollectionLog(
                    pit_id="pit-demo",
                    completed_at=START,
                    residual_height_cm=0,
                    operator_name="합성 동시 기록",
                )
            )
            await writer.commit()
        assert await test_db_session.scalar(count) == before
    assert await test_db_session.scalar(count) == before + 1


async def test_postgresql_query_timeout_releases_connection(
    client, test_engine, test_db_session, monkeypatch
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL query cancellation")
    await login(client)

    async def slow_sql(db, *args):
        await db.execute(text("SELECT pg_sleep(1)"))

    monkeypatch.setattr(history_csv, "QUERY_SECONDS", 0.1)
    monkeypatch.setattr(history_csv, "load_history", slow_sql)
    response = await client.get(LOAD, params=PARAMS)
    assert response.status_code == 503 and response.json()["detail"] == "CSV_QUERY_TIMEOUT"
    assert not test_db_session.in_transaction()
    assert await test_db_session.scalar(text("SELECT 1")) == 1
