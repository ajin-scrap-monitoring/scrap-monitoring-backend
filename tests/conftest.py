"""Pytest configuration, fixtures, and test DB setup."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.app.core.passwords import hash_password
from src.app.db.base import Base
from src.app.db.session import get_db_session
from src.app.main import create_app
from src.app.models.auth import AuthUser

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def test_engine():
    """Create in-memory SQLite engine for test isolation."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="function")
async def test_db_session(test_engine) -> AsyncGenerator[AsyncSession]:
    """Yield test DB session bound to in-memory SQLite."""
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
