"""Preserve receipts/results and reject unsafe replacement definitions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.app.db.migration import current_revisions, require_current_schema, upgrade_schema
from src.app.db.migrations.legacy_index_v1 import INDEX, STAGING, index_state
from src.app.models.edge_v2 import EdgeV2Message


def messages():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "site_id": "synthetic-site",
            "edge_id": "synthetic-edge",
            "message_id": f"m-{i}",
            "message_type": "edge_assessment",
            "config_revision": "synthetic-r1",
            "generated_at": start + timedelta(seconds=i),
            "received_at": start + timedelta(seconds=i),
            "body_hash": f"{i:064x}",
            "payload": {"synthetic": i},
            "compact": i % 20 != 0,
        }
        for i in range(400)
    ]


async def old_schema(engine):
    async with engine.begin() as conn:
        # Test fixtures start on the current head. Recreate only the previous
        # auxiliary index and revision; never downgrade any business tables.
        await conn.execute(text(f"DROP INDEX {INDEX}"))
        await conn.execute(
            text(
                f"CREATE INDEX {INDEX} ON edge_v2_messages "
                "(site_id,edge_id,message_type,compact,generated_at)"
            )
        )
        await conn.execute(
            text("UPDATE alembic_version SET version_num='0008_operations_recipient'")
        )
        await conn.execute(EdgeV2Message.__table__.insert(), messages())


async def install(engine, monkeypatch):
    from src.app.cli import db as cli

    async def no_close():
        pass

    monkeypatch.setattr(cli, "get_engine", lambda: engine)
    monkeypatch.setattr(cli, "close_db", no_close)
    await cli.run("upgrade")


async def test_upgrade_keeps_all_receipts_and_filtered_history(test_engine, monkeypatch):
    if test_engine.dialect.name == "sqlite":
        async with test_engine.begin() as conn:
            from src.app.db.base import Base

            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(upgrade_schema)
    await old_schema(test_engine)
    async with test_engine.connect() as conn:
        before = (
            await conn.execute(select(EdgeV2Message.__table__).order_by(EdgeV2Message.id))
        ).all()
    await install(test_engine, monkeypatch)
    await install(test_engine, monkeypatch)
    async with test_engine.begin() as conn:
        await conn.run_sync(require_current_schema)
        assert await conn.run_sync(current_revisions) == ("0009_legacy_history_index",)
        assert (
            await conn.execute(select(EdgeV2Message.__table__).order_by(EdgeV2Message.id))
        ).all() == before
        ids = (
            await conn.scalars(
                text(
                    "SELECT message_id FROM edge_v2_messages "
                    "WHERE site_id='synthetic-site' AND edge_id='synthetic-edge' "
                    "AND message_type='edge_assessment' AND compact IS FALSE ORDER BY generated_at"
                )
            )
        ).all()
        assert ids == [f"m-{i}" for i in range(0, 400, 20)]
        if conn.dialect.name == "postgresql":
            assert await conn.run_sync(index_state, INDEX) == (True, True)
            assert await conn.run_sync(index_state, STAGING) is None
            await conn.execute(text("SET LOCAL plan_cache_mode=force_generic_plan"))
            await conn.execute(
                text(
                    "PREPARE legacy_lookup(text,text,text) AS SELECT id FROM edge_v2_messages "
                    "WHERE site_id=$1 AND edge_id=$2 AND message_type=$3 "
                    "AND compact IS FALSE ORDER BY generated_at"
                )
            )
            result = (
                await conn.execute(
                    text(
                        "EXECUTE legacy_lookup('synthetic-site','synthetic-edge','edge_assessment')"
                    )
                )
            ).all()
            assert len(result) == 20
            await conn.execute(text("DEALLOCATE legacy_lookup"))
        else:
            definition = await conn.scalar(
                text("SELECT sql FROM sqlite_master WHERE name=:name"), {"name": INDEX}
            )
            assert "WHERE compact IS FALSE" in definition
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(EdgeV2Message.__table__.insert(), messages()[0])


async def test_postgresql_preparation_keeps_valid_staging_on_retry(test_engine, monkeypatch):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent index preparation")
    from src.app.db.legacy_index_prepare import prepare_legacy_index

    await old_schema(test_engine)
    await prepare_legacy_index(test_engine)
    async with test_engine.connect() as conn:
        oid = await conn.scalar(text("SELECT CAST(:name AS regclass)::oid"), {"name": STAGING})
    await prepare_legacy_index(test_engine)
    async with test_engine.connect() as conn:
        again = await conn.scalar(text("SELECT CAST(:name AS regclass)::oid"), {"name": STAGING})
        assert again == oid
    await install(test_engine, monkeypatch)


async def test_postgresql_foreign_staging_is_not_dropped(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL replacement ownership")
    from src.app.db.legacy_index_prepare import prepare_legacy_index

    await old_schema(test_engine)
    async with test_engine.begin() as conn:
        await conn.execute(text(f"CREATE INDEX {STAGING} ON edge_v2_messages (received_at)"))
    with pytest.raises(RuntimeError, match="unexpected definition"):
        await prepare_legacy_index(test_engine)
    async with test_engine.connect() as conn:
        assert await conn.run_sync(index_state, STAGING) == (False, True)
        assert await conn.run_sync(index_state, INDEX) == (False, True)


async def test_postgresql_swap_timeout_preserves_old_index_and_revision(test_engine, monkeypatch):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL transactional DDL lock timeout")
    from sqlalchemy.exc import DBAPIError

    from src.app.db.legacy_index_prepare import prepare_legacy_index

    await old_schema(test_engine)
    await prepare_legacy_index(test_engine)
    async with test_engine.connect() as blocking:
        await blocking.execute(text("SELECT id FROM edge_v2_messages LIMIT 1"))
        with pytest.raises(DBAPIError):
            await install(test_engine, monkeypatch)
        async with test_engine.connect() as reading:
            assert await reading.run_sync(current_revisions) == ("0008_operations_recipient",)
            assert await reading.run_sync(index_state, INDEX) == (False, True)
            assert await reading.run_sync(index_state, STAGING) == (True, True)
            assert await reading.scalar(text("SELECT count(*) FROM edge_v2_messages")) == 400
        await blocking.rollback()
    await install(test_engine, monkeypatch)
    async with test_engine.connect() as reading:
        assert await reading.run_sync(index_state, INDEX) == (True, True)


async def test_postgresql_foreign_original_index_is_not_replaced(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL original index ownership")
    from src.app.db.legacy_index_prepare import prepare_legacy_index

    await old_schema(test_engine)
    async with test_engine.begin() as conn:
        await conn.execute(text(f"DROP INDEX {INDEX}"))
        await conn.execute(text(f"CREATE INDEX {INDEX} ON auth_users (display_name)"))
    with pytest.raises(RuntimeError, match="Original history index"):
        await prepare_legacy_index(test_engine)
    async with test_engine.connect() as conn:
        assert await conn.run_sync(index_state, INDEX, True) == (False, True)
        assert await conn.run_sync(index_state, STAGING) is None
        assert await conn.scalar(text("SELECT count(*) FROM edge_v2_messages")) == 400
