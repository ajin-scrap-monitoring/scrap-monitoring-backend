"""Permanent duplicate/conflict receipts remain intact after bounded metadata reduction."""

import copy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.services.edge_v2_service import digest
from src.app.services.receipt_compaction import compact_receipts, reducible_receipt
from tests.test_compact_lidar_storage import START, healthy, send
from tests.test_compact_lidar_storage import compact_config as compact_config
from tests.test_monitoring_v2 import assessment, status


def stored(index=1):
    payload = {
        "schema_version": "2.0",
        "message_id": f"old-{index}",
        "site_id": "site-demo",
        "edge_id": "edge-demo",
        "generated_at": START.isoformat(),
        "config_revision": "cfg-demo",
        "message_type": "edge_assessment",
        "assessment_type": "STATE",
        "retention": "LATEST_ONLY",
    }
    return EdgeV2Message(
        site_id="site-demo",
        edge_id="edge-demo",
        message_id=f"old-{index}",
        message_type="edge_assessment",
        config_revision="cfg-demo",
        generated_at=START,
        received_at=START + timedelta(seconds=1),
        body_hash=digest({"synthetic": index}),
        payload=payload,
        compact=True,
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("message_id", "wrong"),
        ("site_id", "wrong"),
        ("edge_id", "wrong"),
        ("message_type", "edge_status"),
        ("config_revision", "wrong"),
        ("schema_version", "3.0"),
        ("assessment_type", "EVENT"),
        ("retention", "RAW_EXPIRED"),
        ("generated_at", "broken"),
        ("generated_at", None),
        ("generated_at", "2030-01-01T12:00:00"),
        ("generated_at", "2030-01-02T12:00:00Z"),
    ],
)
def test_mismatched_or_unknown_receipt_is_never_reduced(key, value):
    row = stored()
    row.payload[key] = value
    assert reducible_receipt(row) is None


def test_original_bodies_and_additional_evidence_are_preserved():
    row = stored()
    row.compact = False
    assert reducible_receipt(row) is None
    row.compact = True
    row.payload["extraEvidence"] = "keep"
    assert reducible_receipt(row) is None


async def test_preview_apply_replay_and_rollback_preserve_all_receipt_columns(test_db_session):
    db = test_db_session
    row = stored()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    before = {c.name: copy.deepcopy(getattr(row, c.name)) for c in row.__table__.columns}
    preview = await compact_receipts(db)
    assert preview.scanned == preview.eligible == 1 and preview.changed == 0
    assert preview.serialized_json_bytes_saved > 100
    assert row.payload == before["payload"]
    result = await compact_receipts(db, apply=True)
    assert result.changed == 1
    await db.flush()
    await db.rollback()
    await db.refresh(row)
    assert row.payload == before["payload"]
    await compact_receipts(db, apply=True)
    await db.commit()
    await db.refresh(row)
    for key, value in before.items():
        if key != "payload":
            assert getattr(row, key) == value
    assert set(row.payload) == {"schema_version", "retention", "assessment_type"}
    assert (await compact_receipts(db, apply=True)).changed == 0
    assert await db.scalar(select(func.count()).select_from(EdgeV2Message)) == 1


async def test_bounded_cursor_skips_original_and_large_bodies_without_loading_them(test_db_session):
    db = test_db_session
    original = stored(1)
    original.compact = False
    original.payload["points"] = "x" * 100000
    extra = stored(2)
    extra.payload["extra"] = "x" * 100000
    final = stored(3)
    db.add_all([original, extra, final])
    await db.commit()
    first = await compact_receipts(db, batch_size=2, apply=True)
    assert first.scanned == 2 and first.changed == 0 and not first.exhausted
    second = await compact_receipts(db, after_id=first.next_after_id, batch_size=2, apply=True)
    assert second.scanned == second.changed == 1 and second.exhausted
    assert len(original.payload["points"]) == len(extra.payload["extra"]) == 100000


async def test_new_and_legacy_receipts_share_original_ack_and_conflict_checks(
    client, test_db_session, compact_config
):
    db = test_db_session
    clock = compact_config
    await healthy(client, clock)
    value = assessment("receipt-state", 0.5, START + timedelta(seconds=1))
    first = await send(client, value, clock)
    row = await db.scalar(
        select(EdgeV2Message).where(EdgeV2Message.message_id == value["message_id"])
    )
    assert set(row.payload) == {"schema_version", "retention", "assessment_type"}
    pointer = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "state"))
    assert pointer.payload == value
    repeated = await send(client, value, clock, START + timedelta(days=400))
    assert repeated.status_code == 200 and repeated.json()["duplicate"] is True
    assert repeated.json()["received_at"] == first.json()["received_at"]
    changed = copy.deepcopy(value)
    changed["reason_codes"] = ["OTHER_REASON"]
    conflict = await send(client, changed, clock)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    await db.refresh(row)
    # Reconstruct the old redundant format to exercise compatibility after reduction.
    row.payload = {
        **row.payload,
        **{
            k: value[k]
            for k in (
                "message_id",
                "site_id",
                "edge_id",
                "generated_at",
                "config_revision",
                "message_type",
            )
        },
    }
    await db.commit()
    assert (await compact_receipts(db, apply=True)).changed == 1
    await db.commit()
    assert (await send(client, value, clock)).json()["received_at"] == first.json()["received_at"]
    assert (await send(client, changed, clock)).status_code == 409


async def test_artifact_conflict_under_new_message_id_is_still_detected(
    client, test_db_session, compact_config
):
    value = status("immutable-status", START, 1)
    assert (await send(client, value, compact_config)).status_code == 201
    changed = copy.deepcopy(value)
    changed["message_id"] = "new-id"
    changed["reason_codes"] = ["OTHER"]
    response = await send(client, changed, compact_config)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACT_CONFLICT"
    assert await test_db_session.scalar(select(func.count()).select_from(EdgeV2Artifact)) == 1


async def test_postgresql_locked_rows_skip_and_reconsider_on_next_sweep(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock semantics")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([stored(1), stored(2)])
        await db.commit()
    async with factory() as first, factory() as second:
        await first.scalar(
            select(EdgeV2Message).order_by(EdgeV2Message.id).limit(1).with_for_update()
        )
        result = await compact_receipts(second, apply=True)
        await second.commit()
        assert result.changed == 1
        await first.rollback()
        assert (await compact_receipts(second, apply=True)).changed == 1


async def test_stale_session_does_not_overwrite_new_evidence(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as first, factory() as second:
        row = stored()
        first.add(row)
        await first.commit()
        changed = await second.get(EdgeV2Message, row.id)
        changed.payload = {**changed.payload, "additionalEvidence": "preserve"}
        await second.commit()
        assert "additionalEvidence" not in row.payload
        assert (await compact_receipts(first, apply=True)).changed == 0
        await first.commit()
        await first.refresh(row)
        assert row.payload["additionalEvidence"] == "preserve"


@pytest.mark.parametrize("options", [{"after_id": -1}, {"batch_size": 0}, {"batch_size": 1001}])
async def test_invalid_bounds_fail_without_writes(test_db_session, options):
    with pytest.raises(ValueError):
        await compact_receipts(test_db_session, apply=True, **options)


async def test_apply_requires_compact_configuration(test_db_session, monkeypatch):
    from src.app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "EDGE_V2_COMPACT_STORAGE_ENABLED", False)
    with pytest.raises(ValueError):
        await compact_receipts(test_db_session, apply=True)
    assert (await compact_receipts(test_db_session)).changed == 0


def test_cli_failure_does_not_expose_credentials(monkeypatch, capsys):
    from src.app.cli import receipt_compaction as cli

    async def failed(**kwargs):
        raise RuntimeError("synthetic-private-credential SQL body")

    monkeypatch.setattr(cli, "run", failed)
    monkeypatch.setattr("sys.argv", ["receipt_compaction", "--dry-run"])
    with pytest.raises(SystemExit):
        cli.main()
    output = capsys.readouterr().out
    assert "RECEIPT_COMPACTION_FAILED" in output
    assert "synthetic-private-credential" not in output and "SQL body" not in output
