"""Verify committed API data survives new app/session instances and pool disposal."""

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.db.session import get_db_session
from src.app.main import create_app


def storage_app(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db_session] = session
    return app


async def test_account_and_policy_survive_new_app_and_connections(test_engine):
    async with AsyncClient(
        transport=ASGITransport(app=storage_app(test_engine)), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/api/v1/session", json={"username": "admin", "password": "admin1234"}
        )
        assert login.status_code == 200
        cookie = login.cookies["scrap_session"]
        csrf = login.json()["csrfToken"]
        original = await client.get("/api/v1/settings/alerts")
        payload = {key: value for key, value in original.json().items() if key != "version"}
        payload.update(sendDelayMinutes=7, eventTypes=["device_error"])
        saved = await client.put(
            "/api/v1/settings/alerts",
            json=payload,
            headers={"X-CSRF-Token": csrf, "If-Match": original.headers["etag"]},
        )
        assert saved.status_code == 200
    # PostgreSQL persists after pool disposal. SQLite in-memory databases exist
    # only while their connection lives, so retain that pool in the ordinary suite.
    # Each HTTP request still uses its own database session and a recreated app.
    if test_engine.dialect.name == "postgresql":
        await test_engine.dispose()
    async with AsyncClient(
        transport=ASGITransport(app=storage_app(test_engine)), base_url="http://testserver"
    ) as client:
        client.cookies.set("scrap_session", cookie)
        current = await client.get("/api/v1/session")
        assert current.status_code == 200
        assert current.json()["csrfToken"] == csrf
        settings = await client.get("/api/v1/settings/alerts")
        assert settings.json() == saved.json()
        assert settings.headers["etag"] == saved.headers["etag"]
        assert (
            await client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf})
        ).status_code == 204
        assert (await client.get("/api/v1/session")).status_code == 401
