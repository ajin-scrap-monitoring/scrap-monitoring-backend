"""Persistent auth behavior: secrets, bootstrap, revocation, expiry and storage failures."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import get_settings
from src.app.core.passwords import hash_password, verify_password
from src.app.models.auth import AuthSession, AuthUser
from src.app.services.auth_bootstrap import bootstrap_accounts
from src.app.services.session_service import SessionService, session_service, token_digest


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("OPERATOR_PASSWORD", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def login(client, password="admin1234", persistent=False):
    r = await client.post(
        "/api/v1/session",
        json={"username": "admin", "password": password, "persistent": persistent},
    )
    assert r.status_code == 200, r.text
    return r


async def empty_accounts(db):
    await db.execute(delete(AuthSession))
    await db.execute(delete(AuthUser))
    await db.commit()


async def test_only_argon2_password_hash_and_cookie_digest_are_persisted(client, test_db_session):
    r = await login(client)
    db = test_db_session
    user = await db.get(AuthUser, "admin")
    cookie = r.cookies["scrap_session"]
    record = await db.scalar(select(AuthSession))
    assert user.password_hash.startswith("$argon2id$")
    assert "m=19456,t=2,p=1" in user.password_hash
    assert "admin1234" not in user.password_hash
    assert record.token_hash == token_digest(cookie) and record.token_hash != cookie
    assert record.csrf_token == r.json()["csrfToken"]
    assert user.password_hash not in r.text and cookie not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"]


@pytest.mark.parametrize("persistent,seconds", [(False, 86400), (True, 2592000)])
async def test_expiry_and_cookie_contract(client, persistent, seconds):
    start = datetime.now(UTC)
    r = await login(client, persistent=persistent)
    expires = datetime.fromisoformat(r.json()["expiresAt"])
    assert seconds - 1 <= (expires - start).total_seconds() <= seconds + 5
    assert f"Max-Age={seconds}" in r.headers["set-cookie"]
    current = await client.get("/api/v1/session")
    assert current.json()["csrfToken"] == r.json()["csrfToken"]
    assert current.headers["cache-control"] == "no-store"


async def test_expired_session_is_denied_and_login_prunes_expired_rows(client, test_db_session):
    r = await login(client)
    old_digest = token_digest(r.cookies["scrap_session"])
    await test_db_session.execute(
        update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await test_db_session.commit()
    r = await client.get("/api/v1/session")
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/media/access")).status_code == 401
    await login(client)
    assert await test_db_session.get(AuthSession, old_digest) is None
    assert await test_db_session.scalar(select(func.count()).select_from(AuthSession)) == 1


async def test_disabled_account_cannot_login_or_use_existing_session(client, test_db_session):
    await login(client)
    await test_db_session.execute(
        update(AuthUser).where(AuthUser.username == "admin").values(is_active=False)
    )
    await test_db_session.commit()
    assert (await client.get("/api/v1/session")).status_code == 401
    r = await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    assert r.status_code == 401


async def test_current_db_role_is_used_for_existing_cookie(client, test_db_session):
    await login(client)
    await test_db_session.execute(
        update(AuthUser).where(AuthUser.username == "admin").values(role="viewer")
    )
    await test_db_session.commit()
    assert (await client.get("/api/v1/session")).json()["user"]["role"] == "viewer"
    r = await client.put(
        "/api/v1/media/cameras/camera-main/recording-policy",
        json={"pre_roll_seconds": 30, "post_roll_seconds": 30, "retention_days": 30},
    )
    assert r.status_code == 403


async def test_new_service_and_independent_db_session_can_read_and_revoke_cookie(
    client, test_engine
):
    r = await login(client)
    token = r.cookies["scrap_session"]
    other = SessionService()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        current = await other.get_session(db, token)
        assert current.user.id == "admin" and current.csrfToken == r.json()["csrfToken"]
        await other.delete_session(db, token)
    assert (await client.get("/api/v1/session")).status_code == 401


async def test_reset_password_revokes_all_sessions_and_environment_cannot_restore_password(
    client, test_db_session, monkeypatch
):
    first = await login(client)
    second = await login(client)
    assert first.cookies["scrap_session"] != second.cookies["scrap_session"]
    assert await session_service.reset_password(
        test_db_session, "admin", "synthetic-new-password-36"
    )
    assert await test_db_session.scalar(select(func.count()).select_from(AuthSession)) == 0
    assert (await client.get("/api/v1/session")).status_code == 401
    monkeypatch.setenv("ADMIN_PASSWORD", "admin1234")
    get_settings.cache_clear()
    await bootstrap_accounts(test_db_session)
    r = await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    assert r.status_code == 401
    await login(client, password="synthetic-new-password-36")
    user = await test_db_session.get(AuthUser, "admin")
    assert user.credential_version == 2


async def test_authenticated_password_snapshot_cannot_create_session_after_reset(test_db_session):
    db = test_db_session
    user = await session_service.authenticate(db, "admin", "admin1234")
    assert user is not None
    await session_service.reset_password(db, "admin", "synthetic-changed-before-login")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await session_service.create_session(db, user)
    assert exc.value.status_code == 401
    assert await db.scalar(select(func.count()).select_from(AuthSession)) == 0


async def test_bootstrap_existing_accounts_does_not_change_hashes(test_db_session, monkeypatch):
    db = test_db_session
    original = (await db.get(AuthUser, "admin")).password_hash
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-different-bootstrap-password")
    get_settings.cache_clear()
    await bootstrap_accounts(db)
    assert (await db.get(AuthUser, "admin")).password_hash == original


@pytest.mark.parametrize("operator", [False, True])
async def test_explicit_bootstrap_accounts_and_roles(test_db_session, monkeypatch, operator):
    db = test_db_session
    await empty_accounts(db)
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-bootstrap-admin-password")
    if operator:
        monkeypatch.setenv("OPERATOR_PASSWORD", "synthetic-bootstrap-operator-password")
    get_settings.cache_clear()
    await bootstrap_accounts(db)
    assert await db.scalar(select(func.count()).select_from(AuthUser)) == (2 if operator else 1)
    admin = await db.get(AuthUser, "admin")
    assert admin.role == "administrator"
    assert await verify_password(admin.password_hash, "synthetic-bootstrap-admin-password")
    if operator:
        assert (await db.get(AuthUser, "operator")).role == "viewer"


@pytest.mark.parametrize("password", [None, "", "short", "x" * 1025])
async def test_empty_db_requires_explicit_strong_password(test_db_session, monkeypatch, password):
    await empty_accounts(test_db_session)
    if password is not None:
        monkeypatch.setenv("ADMIN_PASSWORD", password)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        await bootstrap_accounts(test_db_session)
    assert await test_db_session.scalar(select(func.count()).select_from(AuthUser)) == 0


async def test_invalid_operator_password_does_not_partially_create_admin(
    test_db_session, monkeypatch
):
    await empty_accounts(test_db_session)
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-valid-admin-password")
    monkeypatch.setenv("OPERATOR_PASSWORD", "short")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        await bootstrap_accounts(test_db_session)
    assert await test_db_session.scalar(select(func.count()).select_from(AuthUser)) == 0


async def test_no_implicit_default_account_on_login(client, test_db_session):
    await empty_accounts(test_db_session)
    r = await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    assert r.status_code == 401
    assert await test_db_session.scalar(select(func.count()).select_from(AuthUser)) == 0


async def test_commit_failure_is_safe_and_does_not_issue_cookie(
    client, test_db_session, monkeypatch
):
    db = test_db_session
    original = db.commit

    async def broken():
        raise OperationalError(
            "synthetic SQL", {"secret": "must-not-leak"}, Exception("storage failure")
        )

    monkeypatch.setattr(db, "commit", broken)
    r = await client.post("/api/v1/session", json={"username": "admin", "password": "admin1234"})
    assert r.status_code == 503 and r.headers["retry-after"] == "1"
    assert "must-not-leak" not in r.text and "Set-Cookie" not in r.headers
    assert await db.scalar(select(func.count()).select_from(AuthSession)) == 0
    monkeypatch.setattr(db, "commit", original)
    await login(client)
    assert await db.scalar(select(func.count()).select_from(AuthSession)) == 1


async def test_password_reset_commit_failure_preserves_old_password_and_sessions(
    client, test_db_session, monkeypatch
):
    await login(client)
    db = test_db_session
    original = db.commit

    async def broken():
        raise OperationalError("synthetic reset", {}, Exception("storage failure"))

    monkeypatch.setattr(db, "commit", broken)
    with pytest.raises(OperationalError):
        await session_service.reset_password(db, "admin", "synthetic-failed-reset-password")
    monkeypatch.setattr(db, "commit", original)
    assert (await client.get("/api/v1/session")).status_code == 200
    await login(client)


async def test_invalid_login_body_does_not_echo_password(client):
    secret = "sensitive-marker-" + ("x" * 1025)
    r = await client.post("/api/v1/session", json={"username": "admin", "password": secret})
    assert r.status_code == 422
    assert "sensitive-marker" not in r.text and "password" not in r.text


async def test_malformed_cookie_and_unknown_user_never_raise(client):
    r = await client.get("/api/v1/session", headers={"Cookie": "scrap_session=" + ("x" * 129)})
    assert r.status_code == 401
    r = await client.post(
        "/api/v1/session", json={"username": "unknown-user", "password": "비밀번호"}
    )
    assert r.status_code == 401


async def test_hashes_are_salted_unicode_supported_and_malformed_hash_is_rejected():
    password = "합성-비밀번호-test-36"
    first, second = await hash_password(password), await hash_password(password)
    assert first != second
    assert await verify_password(first, password)
    assert not await verify_password(first, password + "wrong")
    assert not await verify_password("invalid-phc", password)
    assert not await verify_password(None, password)


async def test_password_hashing_does_not_block_request_event_loop(monkeypatch):
    import src.app.core.passwords as module

    started = threading.Event()
    finish = threading.Event()

    class SlowHasher:
        def hash(self, password):
            started.set()
            assert finish.wait(3)
            return "synthetic-hash"

    monkeypatch.setattr(module, "_hasher", SlowHasher())
    task = asyncio.create_task(hash_password("synthetic-password"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0)
    finally:
        finish.set()
    assert await task == "synthetic-hash"


async def test_startup_auth_failure_closes_db_resources(monkeypatch):
    import src.app.main as module

    closed = []

    async def init():
        pass

    class EmptyDb:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            pass

    async def broken(db):
        raise ValueError("synthetic missing bootstrap password")

    async def close():
        closed.append(True)

    monkeypatch.setattr(module, "init_db", init)
    monkeypatch.setattr(module, "get_sessionmaker", lambda: lambda: EmptyDb())
    monkeypatch.setattr(module, "bootstrap_accounts", broken)
    monkeypatch.setattr(module, "close_db", close)
    with pytest.raises(ValueError):
        async with module.lifespan(module.create_app()):
            pass
    assert closed == [True]
