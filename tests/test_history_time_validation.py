"""Equivalent UTC history filters and safe rejection of unrepresentable dates."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select

from src.app.main import create_app
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric

PATHS = ["/api/v1/history/load", "/api/v1/events"]
STAMP = datetime(2026, 1, 1, 3, tzinfo=UTC)


async def seed(db):
    db.add_all(
        [
            ScrapMetric(pit_id="pit-demo", measured_at=STAMP, fill_ratio_percent=50),
            ScrapMetric(pit_id="pit-demo", measured_at=STAMP, is_valid=False, state="ERROR"),
            CollectionLog(
                pit_id="pit-demo",
                completed_at=STAMP,
                residual_height_cm=0,
                operator_name="Synthetic",
            ),
            AlertLog(
                pit_id="pit-demo", triggered_at=STAMP, fill_ratio_percent=95, message="Synthetic"
            ),
        ]
    )
    await db.commit()


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("category", ["all", "collection", "alert", "error"])
@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01T11:00:00+09:00", "2026-01-01T13:00:00+09:00"),
        ("2025-12-31T21:00:00-05:00", "2025-12-31T23:00:00-05:00"),
    ],
)
async def test_same_instant_history_queries_match_across_timezones(
    client, test_db_session, path, category, start, end
):
    await seed(test_db_session)
    params = {"pit_id": "pit-demo", "category": category}
    expected = await client.get(
        path, params={**params, "from": "2026-01-01T02:00:00Z", "to": "2026-01-01T04:00:00Z"}
    )
    actual = await client.get(path, params={**params, "from": start, "to": end})
    assert expected.status_code == actual.status_code == 200
    assert actual.json() == expected.json()
    body = actual.json()
    if path.endswith("load"):
        assert len(body["samples"]) == 2
        assert body["from"].endswith("Z") and body["to"].endswith("Z")
    else:
        assert len(body["items"]) == (3 if category == "all" else 1)
        assert all(item["occurredAt"].endswith("Z") for item in body["items"])
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("path", PATHS)
async def test_naive_query_times_keep_legacy_utc_interpretation(client, test_db_session, path):
    await seed(test_db_session)
    params = {"pit_id": "pit-demo", "from": "2026-01-01T02:00:00", "to": "2026-01-01T04:00:00"}
    naive = await client.get(path, params=params)
    aware = await client.get(
        path, params={**params, "from": params["from"] + "Z", "to": params["to"] + "Z"}
    )
    assert naive.status_code == aware.status_code == 200
    assert naive.json() == aware.json()


@pytest.mark.parametrize("path", PATHS)
async def test_equal_inclusive_endpoints_keep_events_at_the_boundary(client, test_db_session, path):
    await seed(test_db_session)
    response = await client.get(
        path,
        params={
            "pit_id": "pit-demo",
            "from": "2026-01-01T12:00:00+09:00",
            "to": "2026-01-01T03:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    if path.endswith("load"):
        assert len(body["samples"]) == 2 and len(body["eventMarkers"]) == 3
    else:
        assert len(body["items"]) == 3


@pytest.mark.parametrize(
    "path,params",
    [
        (PATHS[0], {"to": "0001-01-01T00:00:00Z"}),
        (PATHS[0], {"from": "0001-01-01T00:00:00+09:00"}),
        (PATHS[0], {"to": "9999-12-31T23:59:59-01:00"}),
        (PATHS[1], {"from": "0001-01-01T00:00:00+09:00"}),
        (PATHS[1], {"to": "9999-12-31T23:59:59-01:00"}),
    ],
)
async def test_out_of_range_dates_are_rejected_before_history_queries(
    client, test_engine, path, params
):
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(path, params=params)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "HISTORY_TIME_OUT_OF_RANGE"}
    assert statements == []


def test_history_openapi_declares_http_and_query_validation_errors():
    schema = create_app().openapi()
    for path in PATHS:
        response = schema["paths"][path]["get"]["responses"]["422"]["content"]["application/json"][
            "schema"
        ]
        assert {choice["$ref"].rsplit("/", 1)[-1] for choice in response["anyOf"]} == {
            "HTTPErrorResponse",
            "RequestValidationErrorResponse",
        }
