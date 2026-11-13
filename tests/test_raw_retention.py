"""Raw expiration preserves durable identities, current observations and historical judgments."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.core.config import Settings
from src.app.db.migrations.raw_retention_v1 import ARTIFACT_INDEX, MESSAGE_INDEX, retention_sql
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Measurement
from src.app.services.edge_v2_service import digest, store_artifacts
from src.app.services.raw_retention import expiration_cutoff, retention_batch
from src.app.services.raw_retention_worker import RawRetentionWorker
from tests.test_edge_v2 import DEVICES, measurement

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def settings(days=30, batch=100, **extra):
    return Settings(
        _env_file=None,
        EDGE_V2_RAW_RETENTION_ENABLED=True,
        EDGE_V2_COMPACT_STORAGE_ENABLED=True,
        EDGE_V2_RAW_RETENTION_DAYS=days,
        EDGE_V2_RAW_RETENTION_BATCH_SIZE=batch,
        **extra,
    )


def artifact(key, age=31, kind="scan_source", **extra):
    time_field = "generated_at" if kind == "assessment_revision" else "acquired_at"
    body = {time_field: (NOW - timedelta(days=age)).isoformat(), "data": [1, 2, 3]}
    body.update(extra)
    return EdgeV2Artifact(
        site_id="site-demo",
        edge_id="edge-demo",
        kind=kind,
        key_hash=digest([key]),
        artifact_key=[key],
        payload=body,
        content_hash=digest(body),
    )


def message(key, age=31, kind="lidar_measurement", **extra):
    body = {"message_type": kind, "data": [1, 2, 3]}
    body.update(extra)
    return EdgeV2Message(
        site_id="site-demo",
        edge_id="edge-demo",
        message_id=key,
        message_type=kind,
        config_revision="cfg-test",
        generated_at=NOW - timedelta(days=age),
        received_at=NOW - timedelta(days=age),
        body_hash=digest(body),
        payload=body,
        compact=True,
    )


def test_settings_default_and_safety():
    defaults = Settings(_env_file=None)
    assert defaults.EDGE_V2_RAW_RETENTION_DAYS == 30
    assert not defaults.EDGE_V2_RAW_RETENTION_ENABLED
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EDGE_V2_RAW_RETENTION_ENABLED=True)


@pytest.mark.parametrize("days", [7, 14, 30, 90, 3650])
def test_days_are_configurable(days):
    assert settings(days).EDGE_V2_RAW_RETENTION_DAYS == days


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_invalid_retention_days_rejected(days):
    with pytest.raises(ValidationError):
        settings(days)


def test_complete_utc_day_boundary_retains_at_least_configured_days():
    assert expiration_cutoff(NOW, 30) == datetime(2026, 9, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        expiration_cutoff(NOW.replace(tzinfo=None), 30)


async def test_preview_has_no_writes_and_apply_preserves_ledger(test_db_session):
    db = test_db_session
    old = artifact("old")
    recent = artifact("recent", age=29)
    receipt = message("receipt")
    db.add_all([old, recent, receipt])
    await db.commit()
    old_hash = old.content_hash
    receipt_hash = receipt.body_hash
    preview, _ = await retention_batch(db, settings=settings(), now=NOW)
    assert preview.dry_run and preview.artifact_bodies == preview.measurement_bodies == 1
    await db.commit()
    await db.refresh(old)
    await db.refresh(receipt)
    assert old.payload["data"] == [1, 2, 3]
    assert receipt.payload["data"] == [1, 2, 3]
    result, _ = await retention_batch(db, apply=True, settings=settings(), now=NOW)
    await db.commit()
    await db.refresh(old)
    await db.refresh(receipt)
    await db.refresh(recent)
    assert not result.dry_run
    assert old.payload["retention"] == receipt.payload["retention"] == "RAW_EXPIRED"
    assert old.content_hash == old_hash and receipt.body_hash == receipt_hash
    assert receipt.message_id == "receipt" and receipt.received_at.replace(
        tzinfo=UTC
    ) == NOW - timedelta(days=31)
    assert recent.payload["data"] == [1, 2, 3]
    assert len((await db.scalars(select(EdgeV2Message))).all()) == 1
    again, _ = await retention_batch(db, apply=True, settings=settings(), now=NOW)
    assert again.candidates_examined == 0


async def test_apply_requires_explicit_enable(test_db_session):
    with pytest.raises(ValueError, match="explicitly enabled"):
        await retention_batch(
            test_db_session, apply=True, settings=Settings(_env_file=None), now=NOW
        )


async def test_30_to_14_to_7_reuses_same_code_and_cannot_restore_deleted_raw(test_db_session):
    db = test_db_session
    rows = [artifact("twenty", 20), artifact("ten", 10), artifact("two", 2)]
    db.add_all(rows)
    await db.commit()
    result, _ = await retention_batch(db, apply=True, settings=settings(30), now=NOW)
    assert result.artifact_bodies == 0
    result, _ = await retention_batch(db, apply=True, settings=settings(14), now=NOW)
    assert result.artifact_bodies == 1
    await db.commit()
    result, _ = await retention_batch(db, apply=True, settings=settings(7), now=NOW)
    assert result.artifact_bodies == 1
    await db.commit()
    result, _ = await retention_batch(db, apply=True, settings=settings(30), now=NOW)
    assert result.artifact_bodies == 0
    for row in rows:
        await db.refresh(row)
    assert [row.payload.get("retention") for row in rows] == ["RAW_EXPIRED", "RAW_EXPIRED", None]


async def test_current_snapshot_and_configured_legacy_originals_are_kept(test_db_session):
    db = test_db_session
    projected = message("projected", age=90)
    legacy = message("legacy-current", age=60)
    other = message("historical", age=120)
    db.add_all([projected, legacy, other])
    await db.flush()
    snapshot = EdgeV2Latest(
        site_id=projected.site_id,
        edge_id=projected.edge_id,
        kind="measurement",
        message_id=projected.message_id,
        generated_at=projected.generated_at,
        payload={"original": "unchanged"},
    )
    db.add(snapshot)
    await db.commit()
    result, _ = await retention_batch(
        db, apply=True, settings=settings(EDGE_V2_DEVICES=DEVICES), now=NOW
    )
    assert result.measurement_bodies == 1
    await db.commit()
    for row in [projected, legacy, other]:
        await db.refresh(row)
    assert projected.payload["data"] == legacy.payload["data"] == [1, 2, 3]
    assert other.payload["retention"] == "RAW_EXPIRED"
    await db.refresh(snapshot)
    assert snapshot.payload == {"original": "unchanged"}


async def test_cloud_removal_keeps_state_history_events_and_configuration(test_db_session):
    db = test_db_session
    state = message(
        "state",
        kind="edge_assessment",
        assessment_type="STATE",
        filtered_point_clouds=[{"points": [1]}],
        fill_state="NORMAL",
        fill_estimate={"ratio": 0.5},
    )
    event = message(
        "event", kind="edge_assessment", assessment_type="EVENT", event={"kind": "COLLECTION"}
    )
    config = artifact("config", kind="configuration")
    status = message("status", kind="edge_status")
    state_artifact = artifact(
        "state-artifact",
        kind="assessment_revision",
        assessment_type="STATE",
        filtered_point_clouds=[{"points": [1]}],
        fill_state="NORMAL",
    )
    event_artifact = artifact(
        "event-artifact",
        kind="assessment_revision",
        assessment_type="EVENT",
        filtered_point_clouds=[{"points": [1]}],
    )
    db.add_all([state, event, config, status, state_artifact, event_artifact])
    await db.commit()
    before = {row.message_id: copy.deepcopy(row.payload) for row in [event, status]}
    original_hash = state_artifact.content_hash
    result, _ = await retention_batch(db, apply=True, settings=settings(), now=NOW)
    assert result.state_clouds == 2 and result.artifact_bodies == 0
    await db.commit()
    for row in [state, event, config, status, state_artifact, event_artifact]:
        await db.refresh(row)
    assert "filtered_point_clouds" not in state.payload and state.payload["fill_estimate"] == {
        "ratio": 0.5
    }
    assert "filtered_point_clouds" not in state_artifact.payload
    assert state_artifact.content_hash == original_hash
    assert event.payload == before["event"] and status.payload == before["status"]
    assert config.payload["data"] == [1, 2, 3] and "filtered_point_clouds" in event_artifact.payload


async def test_rollback_leaves_originals_intact(test_db_session):
    db = test_db_session
    row = artifact("rollback")
    db.add(row)
    await db.commit()
    await retention_batch(db, apply=True, settings=settings(), now=NOW)
    await db.rollback()
    await db.refresh(row)
    assert row.payload["data"] == [1, 2, 3]


async def test_bounded_cursor_skips_malformed_time_without_starving_later_rows(test_db_session):
    db = test_db_session
    invalid = artifact("invalid", acquired_at="2026-09-20Tnot-a-time")
    valid = artifact("valid", age=35)
    valid.payload["acquired_at"] = "2026-09-21T12:00:00Z"
    db.add_all([invalid, valid])
    await db.commit()
    result, cursor = await retention_batch(db, apply=True, settings=settings(batch=1), now=NOW)
    assert result.invalid_times_skipped == 1 and result.artifact_bodies == 0
    await db.commit()
    result, cursor = await retention_batch(
        db, cursor, apply=True, settings=settings(batch=1), now=NOW
    )
    assert result.artifact_bodies == 1
    await db.commit()
    await db.refresh(invalid)
    await db.refresh(valid)
    assert invalid.payload["acquired_at"].endswith("not-a-time")
    assert valid.payload["retention"] == "RAW_EXPIRED"
    result, _ = await retention_batch(db, cursor, apply=True, settings=settings(batch=1), now=NOW)
    assert result.exhausted


@pytest.mark.parametrize("missing_hash", [None, ""])
async def test_missing_hash_is_preserved_before_expiring_artifact(test_db_session, missing_hash):
    db = test_db_session
    row = artifact("no-hash")
    expected = digest(row.payload)
    row.content_hash = missing_hash
    db.add(row)
    await db.commit()
    await retention_batch(db, apply=True, settings=settings(), now=NOW)
    await db.commit()
    await db.refresh(row)
    assert row.content_hash == expected and row.payload["retention"] == "RAW_EXPIRED"


async def test_unknown_scan_time_is_not_replaced_by_an_unrelated_generated_field(test_db_session):
    row = artifact("unknown-time")
    row.payload.pop("acquired_at")
    row.payload["generated_at"] = (NOW - timedelta(days=90)).isoformat()
    test_db_session.add(row)
    await test_db_session.commit()
    await retention_batch(test_db_session, apply=True, settings=settings(), now=NOW)
    await test_db_session.commit()
    await test_db_session.refresh(row)
    assert row.payload["data"] == [1, 2, 3] and "retention" not in row.payload


async def test_original_retransmission_after_expiry_still_passes_artifact_conflict_check(
    test_db_session, monkeypatch
):
    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "false")
    from src.app.core.config import get_settings

    get_settings.cache_clear()
    payload = Measurement.model_validate(measurement("replay"))
    db = test_db_session
    await store_artifacts(db, payload)
    await db.commit()
    await retention_batch(db, apply=True, settings=settings(), now=NOW)
    await db.commit()
    await store_artifacts(db, payload)
    await db.commit()
    assert all(
        row.payload["retention"] == "RAW_EXPIRED"
        for row in (await db.scalars(select(EdgeV2Artifact))).all()
    )
    changed = measurement("changed")
    changed["sensors"][0]["scan"]["points"][0]["distance_mm"] += 1
    from src.app.core.edge_v2_security import EdgeV2Error

    with pytest.raises(EdgeV2Error):
        await store_artifacts(db, Measurement.model_validate(changed))
    get_settings.cache_clear()


async def test_cli_default_run_is_read_only(test_engine, test_db_session, monkeypatch):
    from src.app.cli import raw_retention as cli
    from src.app.db.migration import upgrade_schema

    if test_engine.dialect.name == "sqlite":
        async with test_engine.begin() as connection:
            # Model tables are the unchanged version-6 table schema. The normal
            # migration then adds the auxiliary version-7 lookup indexes.
            await connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            )
            await connection.execute(
                text("INSERT INTO alembic_version VALUES ('0006_email_deliveries')")
            )
            await connection.run_sync(upgrade_schema)
    row = artifact("cli-preview")
    test_db_session.add(row)
    await test_db_session.commit()
    monkeypatch.setattr(cli, "get_engine", lambda: test_engine)
    monkeypatch.setattr(
        cli, "get_sessionmaker", lambda: async_sessionmaker(test_engine, expire_on_commit=False)
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings())
    report = await cli.run(apply=False, max_batches=1)
    assert report["batches_processed"] == 1
    await test_db_session.refresh(row)
    assert row.payload["data"] == [1, 2, 3]


async def test_postgresql_candidate_indexes_are_used(test_db_session):
    db = test_db_session
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL planner integration")
    sql = retention_sql("postgresql")
    await db.execute(text("SET LOCAL enable_seqscan=off"))
    for table, name, predicate, order, condition in [
        (
            "edge_v2_artifacts",
            ARTIFACT_INDEX,
            sql.artifact_where,
            f"{sql.artifact_day}, id",
            f"{sql.artifact_day} < '2026-09-30'",
        ),
        (
            "edge_v2_messages",
            MESSAGE_INDEX,
            sql.message_where,
            "received_at, id",
            "received_at < '2026-09-30T00:00:00Z'",
        ),
    ]:
        rows = await db.execute(
            text(
                f"EXPLAIN SELECT id FROM {table} WHERE {predicate} AND {condition} "
                f"ORDER BY {order} LIMIT 100"
            )
        )
        plan = "\n".join(str(row[0]) for row in rows)
        assert name in plan


async def test_postgresql_concurrent_batches_skip_each_others_locks(test_engine, test_db_session):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock integration")
    test_db_session.add_all([artifact("concurrent-a"), artifact("concurrent-b")])
    await test_db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as first, factory() as second:
        a, _ = await retention_batch(first, apply=True, settings=settings(batch=1), now=NOW)
        b, _ = await retention_batch(second, apply=True, settings=settings(batch=1), now=NOW)
        assert a.artifact_bodies == b.artifact_bodies == 1
        await first.commit()
        await second.commit()
    test_db_session.expire_all()
    rows = (await test_db_session.scalars(select(EdgeV2Artifact))).all()
    assert len(rows) == 2 and all(row.payload["retention"] == "RAW_EXPIRED" for row in rows)


async def test_postgresql_existing_data_migration_allows_ingestion_writer(
    test_engine, test_db_session, monkeypatch
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent index migration integration")
    from src.app.cli import db as cli
    from src.app.db.migration import require_current_schema

    async def no_close():
        pass

    monkeypatch.setattr(cli, "get_engine", lambda: test_engine)
    monkeypatch.setattr(cli, "close_db", no_close)

    row = artifact("migration-existing")
    test_db_session.add(row)
    await test_db_session.commit()
    async with test_engine.begin() as conn:
        for name in [ARTIFACT_INDEX, MESSAGE_INDEX]:
            await conn.execute(text(f"DROP INDEX {name}"))
        await conn.execute(text("UPDATE alembic_version SET version_num='0006_email_deliveries'"))
    started = asyncio.Event()
    writer_ready = asyncio.Event()
    ddl = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("CREATE INDEX CONCURRENTLY"):
            ddl.append(statement)
            started.set()

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)

    async def writer():
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
            session.add(message("migration-live-writer", age=0))
            await session.flush()
            writer_ready.set()
            await asyncio.wait_for(started.wait(), 10)
            await session.commit()

    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(writer_ready.wait(), 5)
        await asyncio.wait_for(cli.run("upgrade"), 20)
        async with test_engine.begin() as conn:
            await conn.run_sync(require_current_schema)
        await task
    finally:
        if not task.done():
            task.cancel()
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert len(ddl) == 2
    await test_db_session.refresh(row)
    assert row.payload["data"] == [1, 2, 3]
    assert (
        await test_db_session.scalar(
            select(EdgeV2Message.id).where(EdgeV2Message.message_id == "migration-live-writer")
        )
        is not None
    )


async def test_worker_disabled_does_not_start(monkeypatch):
    from src.app.services import raw_retention_worker as module

    monkeypatch.setattr(module, "get_settings", lambda: Settings(_env_file=None))
    worker = RawRetentionWorker()
    worker.start()
    assert worker._task is None
    await worker.stop()


async def test_cancelled_worker_rolls_back_its_uncommitted_batch(
    test_engine, test_db_session, monkeypatch
):
    from src.app.services import raw_retention_worker as module

    row = artifact("cancelled-worker")
    test_db_session.add(row)
    await test_db_session.commit()
    started = asyncio.Event()

    async def interrupted(db, *args, **kwargs):
        record = await db.get(EdgeV2Artifact, row.id)
        record.payload = {"retention": "RAW_EXPIRED"}
        await db.flush()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "get_settings", lambda: settings())
    monkeypatch.setattr(
        module, "get_sessionmaker", lambda: async_sessionmaker(test_engine, expire_on_commit=False)
    )
    monkeypatch.setattr(module, "retention_batch", interrupted)
    worker = RawRetentionWorker()
    worker.start()
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(worker.stop(), 5)
    assert worker._task is None
    await test_db_session.refresh(row)
    assert row.payload["data"] == [1, 2, 3]
