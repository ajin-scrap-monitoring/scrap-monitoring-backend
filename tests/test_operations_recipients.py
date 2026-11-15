"""Independent recipient selection, revocation and bounded private cache behavior."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.models.recipient import NotificationRecipientModel
from src.app.operations import recipients as ops

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def snapshot():
    return ops.RecipientSnapshot(
        generated_at=NOW,
        recipients=[ops.OperationsRecipient(id="r", email="private@example.com", version=1)],
    )


async def test_projection_filters_and_empty_success_revokes_cached_targets(test_engine, tmp_path):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        for name, enabled, subscribed, channels in [
            ("selected", True, True, "email"),
            ("disabled", False, True, "email"),
            ("not-selected", True, False, "email"),
            ("no-email", True, True, "sms"),
        ]:
            db.add(
                NotificationRecipientModel(
                    id=name,
                    name=name,
                    team="Test",
                    email=f"{name}@example.com",
                    enabled=enabled,
                    operations_email_enabled=subscribed,
                    channels=channels,
                    subscription_mode="custom",
                    event_types="",
                )
            )
        await db.commit()
    path = tmp_path / "recipients.json"
    selected, cached = await ops.refresh_recipients(factory, path, NOW)
    assert not cached
    assert [r.id for r in selected.recipients] == ["selected"]
    async with factory() as db:
        await db.execute(update(NotificationRecipientModel).values(operations_email_enabled=False))
        await db.commit()
    empty, cached = await ops.refresh_recipients(factory, path, NOW + timedelta(seconds=1))
    assert not cached and empty.recipients == []
    assert ops.read_snapshot(path, NOW + timedelta(seconds=2), 100).recipients == []


@pytest.mark.parametrize("failure", ["down", "timeout"])
async def test_unavailable_db_uses_last_good_cache_without_refreshing_its_age(
    monkeypatch, tmp_path, failure
):
    path = tmp_path / "recipients.json"
    ops.write_snapshot(path, snapshot())
    original = path.read_bytes()

    async def unavailable(*args):
        if failure == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("private DSN must not escape")

    monkeypatch.setattr(ops, "read_recipients", unavailable)
    result, cached = await ops.refresh_recipients(
        None, path, NOW + timedelta(seconds=20), timeout_seconds=0.001
    )
    assert cached and result == snapshot() and path.read_bytes() == original
    with pytest.raises(ops.RecipientCacheError, match="CACHE_UNAVAILABLE"):
        await ops.refresh_recipients(None, path, NOW + timedelta(seconds=86401))


async def test_cancellation_propagates_and_cache_write_failure_does_not_route_stale(
    monkeypatch, tmp_path
):
    path = tmp_path / "recipients.json"
    ops.write_snapshot(path, snapshot())
    monkeypatch.setattr(ops, "read_recipients", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await ops.refresh_recipients(None, path, NOW)
    monkeypatch.setattr(ops, "read_recipients", AsyncMock(return_value=snapshot()))

    def failed(*args):
        raise ops.RecipientCacheError("WRITE_FAILED")

    monkeypatch.setattr(ops, "write_snapshot", failed)
    with pytest.raises(ops.RecipientCacheError, match="WRITE_FAILED"):
        await ops.refresh_recipients(None, path, NOW)


@pytest.mark.parametrize(
    "invalid", ["expired", "future", "permissions", "symlink", "large", "json"]
)
def test_cache_fails_closed_for_invalid_private_snapshot(tmp_path, invalid):
    path = tmp_path / "recipients.json"
    ops.write_snapshot(path, snapshot())
    now = NOW
    if invalid == "expired":
        now += timedelta(seconds=101)
    elif invalid == "future":
        now -= timedelta(seconds=1)
    elif invalid == "permissions":
        path.chmod(0o644)
    elif invalid == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif invalid == "large":
        path.write_bytes(b"x" * (ops.MAX_CACHE_BYTES + 1))
    elif invalid == "json":
        path.write_text("{private@example.com")
    with pytest.raises(ops.RecipientCacheError) as error:
        ops.read_snapshot(path, now, 100)
    assert "private@example.com" not in str(error.value)


def test_atomic_private_cache_and_repr(tmp_path):
    path = tmp_path / "private" / "recipients.json"
    ops.write_snapshot(path, snapshot())
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert ops.read_snapshot(path, NOW + timedelta(seconds=100), 100) == snapshot()
    assert len(list(path.parent.iterdir())) == 1
    assert "private@example.com" not in repr(snapshot())
    assert "private@example.com" not in repr(snapshot().recipients[0])


def test_writer_refuses_symlink_without_modifying_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ops.RecipientCacheError):
        ops.write_snapshot(link, snapshot())
    assert target.read_text() == "preserve"


@pytest.mark.parametrize("max_age,timeout", [(0, 8), (86401, 8), (10, 0), (10, 31)])
async def test_invalid_cache_limits_do_not_query_db(monkeypatch, tmp_path, max_age, timeout):
    query = AsyncMock()
    monkeypatch.setattr(ops, "read_recipients", query)
    with pytest.raises(ops.RecipientCacheError, match="CONFIG_INVALID"):
        await ops.refresh_recipients(
            None, tmp_path / "cache", NOW, max_age_seconds=max_age, timeout_seconds=timeout
        )
    query.assert_not_called()


def test_cli_does_not_print_private_configuration(monkeypatch, capsys, tmp_path):
    from src.app.cli import operations_recipients as cli

    monkeypatch.setattr("sys.argv", ["operations_recipients", "--cache", str(tmp_path / "cache")])
    monkeypatch.setenv("OPS_RECIPIENT_DATABASE_URL", "private-password@example.com")
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "private-password" not in output.out + output.err
    assert "OPERATIONS_RECIPIENT_REFRESH_FAILED" in output.out
    assert '"notificationSent": false' in output.out
