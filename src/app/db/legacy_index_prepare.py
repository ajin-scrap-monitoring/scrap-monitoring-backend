"""Build a replacement without dropping the old lookup or touching receipt rows."""

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.db.migration import current_revisions
from src.app.db.migrations.legacy_index_v1 import (
    COLUMNS,
    INDEX,
    PREDICATE,
    PREVIOUS,
    STAGING,
    index_state,
)


def build_legacy_index(connection: Connection) -> None:
    locked = connection.scalar(
        text("SELECT pg_try_advisory_lock(hashtext('backend_legacy_index_v1'))")
    )
    if not locked:
        raise RuntimeError("Another legacy-index preparation is active")
    try:
        connection.execute(text("SET lock_timeout='5s'"))
        connection.execute(text("SET statement_timeout='15min'"))
        if index_state(connection, INDEX, True) != (True, True):
            raise RuntimeError("Original history index has an unexpected definition")
        state = index_state(connection, STAGING)
        if state is not None:
            if not state[0]:
                raise RuntimeError("Replacement index has an unexpected definition")
            if state[1]:
                return
            # Only this version's invalid staging index may be removed on retry.
            connection.execute(text(f"DROP INDEX CONCURRENTLY {STAGING}"))
        connection.execute(
            text(
                f"CREATE INDEX CONCURRENTLY {STAGING} ON edge_v2_messages "
                f"({COLUMNS}) WHERE {PREDICATE}"
            )
        )
        if index_state(connection, STAGING) != (True, True):
            raise RuntimeError("Replacement index is not valid")
    finally:
        connection.execute(text("RESET statement_timeout"))
        connection.execute(text("RESET lock_timeout"))
        connection.execute(text("SELECT pg_advisory_unlock(hashtext('backend_legacy_index_v1'))"))


async def prepare_legacy_index(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql":
        return
    async with engine.connect() as connection:
        revisions = await connection.run_sync(current_revisions)
        await connection.rollback()
        if revisions != (PREVIOUS,):
            return
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.run_sync(build_legacy_index)
