"""Account isolation, authentication, retries and durable notification acknowledgments."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_monitoring_v2 import DEVICE, SOURCE

from src.app.core.config import get_settings
from src.app.db.base import Base
from src.app.models.auth import AuthSession, AuthUser
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.log import AlertLog
from src.app.models.notification_read import NotificationRead
from src.app.services.notification_service import NotificationService

PATH = "/api/v1/notifications"


async def login(client, username="admin"):
    r = await client.post(
        "/api/v1/session",
        json={
            "username": username,
            "password": "admin1234" if username == "admin" else "operator1234",
        },
    )
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrfToken"]}


async def seed(db, count=1, legacy_read=False):
    rows = [
        AlertLog(
            pit_id="pit-demo",
            triggered_at=datetime.now(UTC),
            fill_ratio_percent=95,
            message="Synthetic alert",
            read_at=datetime.now(UTC) if legacy_read else None,
        )
        for _ in range(count)
    ]
    db.add_all(rows)
    await db.commit()
    return [row.id for row in rows]


async def test_single_read_is_private_idempotent_and_matches_v2(
    client, test_db_session, monkeypatch
):
    monkeypatch.setenv("EDGE_V2_DEVICES", json.dumps([DEVICE]))
    monkeypatch.setenv("EDGE_V2_MONITORING_SOURCES", json.dumps([SOURCE]))
    get_settings.cache_clear()
    try:
        db = test_db_session
        identifier = (await seed(db))[0]
        db.add(
            EdgeV2AlertLink(
                alert_id=identifier,
                pit_id="pit-demo",
                site_id="site-demo",
                edge_id="edge-demo",
                assessment_id="synthetic-assessment",
                message_id="synthetic-message",
                observed_at=datetime.now(UTC),
            )
        )
        await db.commit()
        admin = await login(client)
        r = await client.patch(f"{PATH}/notif-{identifier}", json={"read": True}, headers=admin)
        assert r.status_code == 200
        original = r.json()["readAt"]
        assert original is not None
        assert (
            await client.patch(f"{PATH}/notif-{identifier}", json={"read": True}, headers=admin)
        ).json()["readAt"] == original
        viewer = await login(client, "operator")
        page = (await client.get(PATH)).json()
        assert page["unreadCount"] == 1 and page["items"][0]["readAt"] is None
        v2 = "/api/v2/monitoring/alerts?pit_id=pit-demo"
        assert (await client.get(v2)).json()["items"][0]["readAt"] is None
        assert (
            await client.patch(f"{PATH}/{identifier}", json={"read": True}, headers=viewer)
        ).status_code == 200
        assert (await client.get(v2)).json()["items"][0]["readAt"] is not None
        await login(client)
        assert (await client.get(PATH)).json()["items"][0]["readAt"] == original
        assert (await client.get(v2)).json()["items"][0]["readAt"] == original
        assert await db.scalar(select(func.count()).select_from(NotificationRead)) == 2
        assert (await db.get(AlertLog, identifier)).read_at is None
    finally:
        get_settings.cache_clear()


async def test_read_all_only_changes_current_account_and_not_future_alerts(client, test_db_session):
    await seed(test_db_session, 3)
    headers = await login(client)
    assert (await client.post(PATH + "/read-all", headers=headers)).status_code == 204
    assert (await client.post(PATH + "/read-all", headers=headers)).status_code == 204
    assert (await client.get(PATH)).json()["unreadCount"] == 0
    await seed(test_db_session)
    unread = (await client.get(PATH, params={"unreadOnly": True})).json()
    assert unread["unreadCount"] == unread["page"]["totalItems"] == 1
    await login(client, "operator")
    page = (await client.get(PATH)).json()
    assert page["unreadCount"] == 4
    assert all(row["readAt"] is None for row in page["items"])
    assert await test_db_session.scalar(select(func.count()).select_from(NotificationRead)) == 3


async def test_pagination_unread_total_and_legacy_common_read_are_not_user_state(
    client, test_db_session
):
    await seed(test_db_session, 4, legacy_read=True)
    await login(client, "operator")
    r = await client.get(PATH, params={"unreadOnly": True, "page": 2, "pageSize": 1})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["unreadCount"] == data["page"]["totalItems"] == 4
    assert len(data["items"]) == 1 and data["items"][0]["readAt"] is None
    assert data["page"]["totalPages"] == 4


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", PATH, None),
        ("PATCH", PATH + "/notif-1", {"read": True}),
        ("POST", PATH + "/read-all", None),
    ],
)
async def test_anonymous_notification_access_is_denied(
    client, test_db_session, method, path, payload
):
    await seed(test_db_session)
    assert (await client.request(method, path, json=payload)).status_code == 401
    assert await test_db_session.scalar(select(func.count()).select_from(NotificationRead)) == 0


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("PATCH", PATH + "/notif-1", {"read": True}),
        ("POST", PATH + "/read-all", None),
    ],
)
@pytest.mark.parametrize("token", [None, "wrong", "other-session"])
async def test_notification_mutations_require_own_csrf(
    client, test_db_session, method, path, payload, token
):
    await seed(test_db_session)
    previous = await login(client)
    await login(client)
    headers = previous if token == "other-session" else {"X-CSRF-Token": token} if token else {}
    assert (await client.request(method, path, json=payload, headers=headers)).status_code == 403
    assert await test_db_session.scalar(select(func.count()).select_from(NotificationRead)) == 0


@pytest.mark.parametrize(
    "identifier", ["notif-999", "notif-" + "9" * 100, "notif-٠١", "notif--1", "notif-2147483648"]
)
async def test_unknown_or_out_of_range_ids_are_safe(client, identifier):
    headers = await login(client)
    assert (
        await client.patch(PATH + "/" + identifier, json={"read": True}, headers=headers)
    ).status_code == 404


@pytest.mark.parametrize("invalid", ["expired", "disabled", "logout"])
async def test_invalid_sessions_cannot_read_or_change_notifications(
    client, test_db_session, invalid
):
    await seed(test_db_session)
    headers = await login(client)
    db = test_db_session
    if invalid == "expired":
        session = await db.scalar(select(AuthSession))
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    elif invalid == "disabled":
        user = await db.get(AuthUser, "admin")
        user.is_active = False
        await db.commit()
    else:
        assert (await client.delete("/api/v1/session", headers=headers)).status_code == 204
    assert (await client.get(PATH)).status_code == 401
    assert (await client.post(PATH + "/read-all", headers=headers)).status_code == 401
    assert await db.scalar(select(func.count()).select_from(NotificationRead)) == 0


@pytest.mark.parametrize("all_reads", [False, True])
async def test_commit_failure_leaves_all_read_state_unchanged(
    client, test_db_session, monkeypatch, all_reads
):
    identifier = (await seed(test_db_session, 2))[0]
    headers = await login(client)
    db = test_db_session
    original = db.commit

    async def broken():
        raise OperationalError("synthetic SQL", {"secret": "must-not-leak"}, Exception("failed"))

    monkeypatch.setattr(db, "commit", broken)
    response = (
        await client.post(PATH + "/read-all", headers=headers)
        if all_reads
        else await client.patch(f"{PATH}/notif-{identifier}", json={"read": True}, headers=headers)
    )
    assert response.status_code == 503 and "must-not-leak" not in response.text
    monkeypatch.setattr(db, "commit", original)
    assert (await client.get(PATH)).json()["unreadCount"] == 2
    assert await db.scalar(select(func.count()).select_from(NotificationRead)) == 0


async def test_request_username_cannot_change_another_account(client, test_db_session):
    identifier = (await seed(test_db_session))[0]
    headers = await login(client, "operator")
    r = await client.patch(
        f"{PATH}/notif-{identifier}?username=admin",
        json={"read": True, "username": "admin"},
        headers=headers,
    )
    assert r.status_code == 200
    rows = (await test_db_session.scalars(select(NotificationRead))).all()
    assert len(rows) == 1 and rows[0].username == "operator"


async def test_file_reopen_and_concurrent_retries_keep_first_read(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'reads.db'}"
    engine = create_async_engine(url)
    service = NotificationService()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all(
                [
                    AuthUser(
                        username=name,
                        display_name=name,
                        role="viewer",
                        password_hash="synthetic-unused-hash",
                    )
                    for name in ("alpha", "beta")
                ]
            )
            await db.commit()
            identifier = (await seed(db))[0]
            first = await service.update_notification(f"notif-{identifier}", True, db, "alpha")

        async def retry(all_reads):
            async with factory() as db:
                if all_reads:
                    await service.mark_all_as_read(db, "alpha")
                else:
                    await service.update_notification(f"notif-{identifier}", True, db, "alpha")

        await asyncio.gather(retry(False), retry(True), retry(False))
    finally:
        await engine.dispose()
    reopened = create_async_engine(url)
    try:
        factory = async_sessionmaker(reopened, expire_on_commit=False)
        async with factory() as db:
            page = await service.list_notifications(False, 1, 10, db, "alpha")
            assert page.unreadCount == 0 and page.items[0].readAt == first.readAt
            other = await service.list_notifications(False, 1, 10, db, "beta")
            assert other.unreadCount == 1 and other.items[0].readAt is None
            assert await db.scalar(select(func.count()).select_from(NotificationRead)) == 1
    finally:
        await reopened.dispose()
