"""Async Alembic environment, also usable with an existing run_sync connection."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from src.app import models
from src.app.db.migration import guard_upgrade
from src.app.db.session import close_db, get_engine

config = context.config


def migrate(connection: Connection) -> None:
    guard_upgrade(connection, bool(config.attributes.get("adopt_legacy", False)))
    context.configure(
        connection=connection, target_metadata=models.AuthUser.metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def migrate_async() -> None:
    try:
        async with get_engine().begin() as connection:
            await connection.run_sync(migrate)
    finally:
        await close_db()


if context.is_offline_mode():
    raise RuntimeError("Use online migrations so the existing database schema can be checked")
else:
    connection = config.attributes.get("connection")
    if connection is None:
        asyncio.run(migrate_async())
    else:
        migrate(connection)
