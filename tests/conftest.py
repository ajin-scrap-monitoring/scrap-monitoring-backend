"""Pytest configuration, fixtures, and test DB setup."""

import asyncio
import os
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.app.core.passwords import hash_password
from src.app.db.base import Base
from src.app.db.migration import require_current_schema, upgrade_schema
from src.app.db.session import get_db_session
from src.app.main import create_app
from src.app.models.auth import AuthUser

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def pytest_addoption(parser):
    parser.addoption(
        "--postgresql", action="store_true", help="Use isolated PostgreSQL schemas for DB fixtures"
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--postgresql"):
        return
    # Run only tests actually connected to the PostgreSQL fixture. SQLite-specific
    # file/reopen tests remain in the ordinary suite, never counted as PG coverage.
    selected = [item for item in items if "test_engine" in item.fixturenames]
    deselected = [item for item in items if "test_engine" not in item.fixturenames]
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def test_engine(request):
    """Create in-memory SQLite engine for test isolation.

    PostgreSQL mode instead migrates a unique schema in an explicit test DB.
    """
    postgres = request.config.getoption("--postgresql")
    admin = None
    schema = "test_" + uuid4().hex
    if postgres:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url or not url.startswith("postgresql+asyncpg://"):
            pytest.fail("--postgresql requires TEST_POSTGRES_URL for a dedicated test DB")
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
        )
    try:
        async with engine.begin() as conn:
            if postgres:
                await conn.run_sync(upgrade_schema)
                await conn.run_sync(require_current_schema)
            else:
                await conn.run_sync(Base.metadata.create_all)
        # Legacy test credentials are synthetic fixtures, never application defaults.
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all(
                [
                    AuthUser(
                        username="admin",
                        display_name="관리자",
                        role="administrator",
                        password_hash=await hash_password("admin1234"),
                    ),
                    AuthUser(
                        username="operator",
                        display_name="현장 운영자",
                        role="viewer",
                        password_hash=await hash_password("operator1234"),
                    ),
                ]
            )
            await db.commit()

        yield engine

    finally:
        await engine.dispose()
        if admin is not None:
            try:
                async with admin.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                await admin.dispose()


@pytest.fixture(scope="function")
async def test_db_session(test_engine) -> AsyncGenerator[AsyncSession]:
    """Yield test DB session bound to the isolated fixture engine."""
    session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
async def client(test_db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Async test client with dependency override for DB session."""
    app = create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        yield test_db_session

    app.dependency_overrides[get_db_session] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
