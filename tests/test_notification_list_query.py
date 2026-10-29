"""Bounded notification pages, shared counts and read-only storage error recovery."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from test_notification_reads import login, seed

from src.app.models.log import AlertLog
from src.app.models.notification_read import NotificationRead

PATH = "/api/v1/notifications"
LARGE_PAGE = 9223372036854775808


async def sample(db):
    identifiers = await seed(db, 4, legacy_read=True)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    for row in (await db.scalars(select(AlertLog))).all():
        row.triggered_at = stamp
    reads = [("admin", identifiers[0]), ("admin", identifiers[1]), ("operator", identifiers[3])]
    db.add_all(
        [
            NotificationRead(username=user, alert_id=identifier, read_at=stamp)
            for user, identifier in reads
        ]
    )
    await db.commit()
    return identifiers, {"admin": set(identifiers[:2]), "operator": {identifiers[3]}}


def observe_alert_queries(statements):
    def observe(conn, cursor, statement, parameters, context, executemany):
        if "from alert_logs" in statement.lower():
            statements.append(statement.lower())

    return observe


@pytest.mark.parametrize("username", ["admin", "operator"])
@pytest.mark.parametrize("unread_only", [False, True])
@pytest.mark.parametrize("page", [1, 2, 3, LARGE_PAGE])
async def test_notification_pages_share_counts_and_preserve_user_reads(
    client, test_db_session, test_engine, username, unread_only, page
):
    identifiers, read_ids = await sample(test_db_session)
    legacy = {
        row.id: row.read_at for row in (await test_db_session.scalars(select(AlertLog))).all()
    }
    await login(client, username)
    ordered = sorted(identifiers, reverse=True)
    expected = [
        identifier
        for identifier in ordered
        if not unread_only or identifier not in read_ids[username]
    ]
    expected_page = expected[(page - 1) * 2 : page * 2]
    statements = []
    observe = observe_alert_queries(statements)
    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(
            PATH, params={"page": page, "pageSize": 2, "unreadOnly": unread_only}
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["page"] == {
        "page": page,
        "pageSize": 2,
        "totalItems": len(expected),
        "totalPages": (len(expected) + 1) // 2,
    }
    assert data["unreadCount"] == 4 - len(read_ids[username])
    assert [item["id"] for item in data["items"]] == [
        f"notif-{identifier}" for identifier in expected_page
    ]
    for item, identifier in zip(data["items"], expected_page, strict=True):
        assert (item["readAt"] is not None) == (identifier in read_ids[username])
    assert len(statements) == (2 if expected_page else 1)
    assert sum("count(" in statement for statement in statements) == 1
    if not expected_page:
        assert all("offset" not in statement for statement in statements)
    assert await test_db_session.scalar(select(func.count()).select_from(NotificationRead)) == 3
    assert {
        row.id: row.read_at for row in (await test_db_session.scalars(select(AlertLog))).all()
    } == legacy


@pytest.mark.parametrize("unread_only", [False, True])
@pytest.mark.parametrize("page", [1, LARGE_PAGE])
async def test_empty_notification_pages_need_one_count_query(
    client, test_engine, unread_only, page
):
    await login(client, "operator")
    statements = []
    observe = observe_alert_queries(statements)
    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(PATH, params={"page": page, "unreadOnly": unread_only})
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "page": {"page": page, "pageSize": 10, "totalItems": 0, "totalPages": 0},
        "unreadCount": 0,
    }
    assert len(statements) == 1 and "count(" in statements[0] and "offset" not in statements[0]


@pytest.mark.parametrize("unread_only", [False, True])
@pytest.mark.parametrize("failure_step", [1, 2])
async def test_notification_query_failure_is_503_without_changing_reads(
    client, test_db_session, monkeypatch, unread_only, failure_step
):
    await sample(test_db_session)
    await login(client)
    params = {"unreadOnly": unread_only}
    original_response = await client.get(PATH, params=params)
    assert original_response.status_code == 200
    execute, rollback = test_db_session.execute, test_db_session.rollback
    queries = rollbacks = 0
    failure_raised = False

    async def fail_query(statement, *args, **kwargs):
        nonlocal queries, failure_raised
        if "alert_logs" in str(statement):
            queries += 1
            if queries == failure_step:
                failure_raised = True
                raise OperationalError("synthetic query", {}, Exception("private-database-detail"))
        return await execute(statement, *args, **kwargs)

    async def count_rollback():
        nonlocal rollbacks
        if failure_raised:
            rollbacks += 1
        await rollback()

    monkeypatch.setattr(test_db_session, "execute", fail_query)
    monkeypatch.setattr(test_db_session, "rollback", count_rollback)
    response = await client.get(PATH, params=params)
    assert response.status_code == 503
    assert response.json() == {"detail": "Notification storage unavailable"}
    assert "private-database-detail" not in response.text
    assert rollbacks == 1
    monkeypatch.setattr(test_db_session, "execute", execute)
    monkeypatch.setattr(test_db_session, "rollback", rollback)
    recovered = await client.get(PATH, params=params)
    assert recovered.status_code == 200 and recovered.json() == original_response.json()
    assert await test_db_session.scalar(select(func.count()).select_from(NotificationRead)) == 3
