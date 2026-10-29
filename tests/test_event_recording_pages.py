"""Recording event pagination boundaries without media calls or DB writes."""

import pytest
from sqlalchemy import event, func, select
from test_daily_recordings import login
from test_event_recordings import API, BASE
from test_event_recordings import archive as archive

from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric

LARGE_PAGE = 9223372036854775808


def observe_queries(statements):
    def observe(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if " as event_id" in sql and sql.lstrip().startswith("select"):
            statements.append(sql)

    return observe


async def sample(db):
    groups = {"alert": [], "collection": [], "error": []}
    for _ in range(3):
        groups["alert"].append(
            AlertLog(
                pit_id="pit-demo", triggered_at=BASE, fill_ratio_percent=95, message="Synthetic"
            )
        )
        groups["collection"].append(
            CollectionLog(
                pit_id="pit-demo",
                completed_at=BASE,
                residual_height_cm=0,
                operator_name="Synthetic",
            )
        )
        groups["error"].append(
            ScrapMetric(pit_id="pit-demo", measured_at=BASE, is_valid=False, state="ERROR")
        )
    db.add_all([row for rows in groups.values() for row in rows])
    await db.commit()
    prefixes = {"alert": "evt-alt", "collection": "evt-col", "error": "evt-err"}
    result = {
        category: sorted([f"{prefixes[category]}-{row.id}" for row in rows], reverse=True)
        for category, rows in groups.items()
    }
    result["all"] = sorted([key for keys in result.values() for key in keys], reverse=True)
    return result


@pytest.mark.parametrize("category", ["all", "collection", "alert", "error"])
@pytest.mark.parametrize("page", [1, 2, 6, LARGE_PAGE])
async def test_recording_pages_keep_counts_order_and_skip_absent_rows(
    client, test_db_session, test_engine, archive, monkeypatch, category, page
):
    identifiers = (await sample(test_db_session))[category]
    await login(client)
    expected = identifiers[(page - 1) * 2 : page * 2]

    async def forbidden_summary(*args):
        pytest.fail("An absent page must not request playback summaries")

    monkeypatch.setattr(
        "src.app.services.event_playback_summary.add_playback_summaries", forbidden_summary
    )
    statements = []
    observe = observe_queries(statements)
    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(
            API,
            params={
                "category": category,
                "page": page,
                "pageSize": 2,
                "from": BASE.isoformat(),
                "include_playback": not expected,
            },
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["page"] == {
        "page": page,
        "pageSize": 2,
        "totalItems": len(identifiers),
        "totalPages": (len(identifiers) + 1) // 2,
    }
    assert [item["eventId"] for item in body["items"]] == expected
    assert len(statements) == (2 if expected else 1)
    if not expected:
        assert statements[0].startswith("select count(")
    assert archive["requests"] == []
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0
    for model in (AlertLog, CollectionLog, ScrapMetric):
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 3


@pytest.mark.parametrize("page", [1, LARGE_PAGE])
@pytest.mark.parametrize("include_playback", [False, True])
async def test_empty_recording_pages_keep_metadata_without_row_or_media_queries(
    client, test_engine, archive, monkeypatch, page, include_playback
):
    await login(client)

    async def forbidden_summary(*args):
        pytest.fail("An empty page must not request playback summaries")

    monkeypatch.setattr(
        "src.app.services.event_playback_summary.add_playback_summaries", forbidden_summary
    )
    statements = []
    observe = observe_queries(statements)
    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(
            API, params={"page": page, "include_playback": include_playback}
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["page"] == {"page": page, "pageSize": 20, "totalItems": 0, "totalPages": 0}
    assert len(statements) == 1 and statements[0].startswith("select count(")
    assert archive["requests"] == []
