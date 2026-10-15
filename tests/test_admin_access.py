"""Administration permissions must fail before reading or changing business data."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.app.models.auth import AuthSession, AuthUser
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog
from src.app.models.recipient import NotificationRecipientModel

SETTINGS = {
    "collectionThresholdPercent": 90,
    "preCollectionAlert": {"enabled": True, "thresholdPercent": 75},
    "sendDelayMinutes": 0,
    "repeatIntervalMinutes": 30,
    "maximumRepeatCount": 3,
    "recoveryNotificationEnabled": True,
    "eventTypes": ["collection_required"],
}
RECIPIENT = {
    "name": "Synthetic recipient",
    "team": "Synthetic team",
    "email": "synthetic@example.com",
    "phone": "010-0000-0000",
    "enabled": True,
    "channels": ["email"],
    "subscriptionMode": "global",
    "eventTypes": ["collection_required"],
}
CHANGES = [
    ("PUT", "/api/v1/settings/alerts", SETTINGS),
    ("POST", "/api/v1/notification-recipients", RECIPIENT),
    ("PATCH", "/api/v1/notification-recipients/missing", {"enabled": False}),
    ("POST", "/api/v1/notifications/test", {"recipientId": "missing", "channels": ["email"]}),
    ("PUT", "/api/v1/config/pits/pit-new", {"warning_threshold_percent": 70}),
]
ADMIN_READS = [
    ("GET", "/api/v1/settings/alerts", None),
    ("GET", "/api/v1/notification-recipients", None),
]
READS = ADMIN_READS + [
    ("GET", "/api/v1/config/pits", None),
    ("GET", "/api/v1/config/pits/missing", None),
]


async def login(client, username="admin"):
    result = await client.post(
        "/api/v1/session",
        json={
            "username": username,
            "password": "admin1234" if username == "admin" else "operator1234",
        },
    )
    assert result.status_code == 200
    return result.json()["csrfToken"]


async def no_business_rows(db):
    for model in (PitConfig, NotificationRecipientModel, AlertLog):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("method,path,payload", READS + CHANGES)
async def test_anonymous_requests_do_not_reach_business_data(
    client, test_db_session, method, path, payload
):
    result = await client.request(method, path, json=payload)
    assert result.status_code == 401
    await no_business_rows(test_db_session)


@pytest.mark.parametrize("method,path,payload", ADMIN_READS + CHANGES)
async def test_viewer_cannot_read_admin_data_or_mutate_settings(
    client, test_db_session, method, path, payload
):
    csrf = await login(client, "operator")
    result = await client.request(method, path, json=payload, headers={"X-CSRF-Token": csrf})
    assert result.status_code == 403
    await no_business_rows(test_db_session)


@pytest.mark.parametrize("method,path,payload", CHANGES)
@pytest.mark.parametrize("token", [None, "wrong", "잘못된 토큰"])
async def test_admin_changes_require_matching_csrf(
    client, test_db_session, method, path, payload, token
):
    await login(client)
    headers = {"X-CSRF-Token": token} if token is not None else {}
    if token and not token.isascii():
        headers = {b"X-CSRF-Token": token.encode("utf-8")}
    result = await client.request(method, path, json=payload, headers=headers)
    assert result.status_code == 403
    await no_business_rows(test_db_session)


async def test_csrf_from_another_admin_session_is_rejected(client, test_db_session):
    previous = await login(client)
    current = await login(client)
    assert previous != current
    result = await client.put(
        "/api/v1/config/pits/pit-new",
        json={"name": "Synthetic pit"},
        headers={"X-CSRF-Token": previous},
    )
    assert result.status_code == 403
    await no_business_rows(test_db_session)


async def test_viewer_can_read_pit_configuration(client):
    await login(client, "operator")
    result = await client.get("/api/v1/config/pits")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/config/pits/pit-01")).status_code == 200
    assert (await client.get("/api/v1/config/pits/missing")).status_code == 404


@pytest.mark.parametrize("invalid", ["expired", "disabled", "logged_out", "malformed"])
async def test_invalid_sessions_cannot_use_administration(client, test_db_session, invalid):
    csrf = await login(client)
    db = test_db_session
    if invalid == "expired":
        session = await db.scalar(select(AuthSession))
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    elif invalid == "disabled":
        user = await db.get(AuthUser, "admin")
        user.is_active = False
        await db.commit()
    elif invalid == "logged_out":
        assert (
            await client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf})
        ).status_code == 204
    else:
        client.cookies.set("scrap_session", "invalid-session")
    assert (await client.get("/api/v1/settings/alerts")).status_code == 401
    assert (
        await client.put(
            "/api/v1/config/pits/pit-new",
            json={"name": "Synthetic pit"},
            headers={"X-CSRF-Token": csrf},
        )
    ).status_code == 401
    await no_business_rows(db)


async def test_db_role_demotion_immediately_removes_admin_access(client, test_db_session):
    csrf = await login(client)
    user = await test_db_session.get(AuthUser, "admin")
    user.role = "viewer"
    await test_db_session.commit()
    assert (await client.get("/api/v1/notification-recipients")).status_code == 403
    assert (
        await client.put(
            "/api/v1/config/pits/pit-new",
            json={"name": "Synthetic pit"},
            headers={"X-CSRF-Token": csrf},
        )
    ).status_code == 403
    await no_business_rows(test_db_session)


async def test_admin_can_create_config_and_read_private_settings(client):
    csrf = await login(client)
    response = await client.put(
        "/api/v1/config/pits/pit-new",
        json={"name": "Synthetic pit"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "pit-new"
    assert response.headers["cache-control"] == "no-store"
    for path in ("/api/v1/settings/alerts", "/api/v1/notification-recipients"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
