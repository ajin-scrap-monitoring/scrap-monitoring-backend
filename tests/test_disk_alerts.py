"""Capacity checks and durable notification intent without real emails or a database."""

import json
import os
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.app.operations import disk_alerts as disk
from src.app.services.gmail_sender import GmailError


def config(**changes):
    return disk.DiskAlertConfig.model_validate(
        {
            "enabled": True,
            "recipients": ["operator@example.com"],
            "targets": [{"name": "database", "path": "/synthetic"}],
            **changes,
        }
    )


@pytest.fixture
def state_path(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory / "state.json"


def usage(monkeypatch, percent):
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: os.statvfs_result(
            (4096, 4096, 100, 100 - percent, 100 - percent, 100, 90, 90, 0, 255)
        ),
    )


async def test_warn_dedup_escalation_hysteresis_and_single_recovery(monkeypatch, state_path):
    sender = AsyncMock()
    usage(monkeypatch, 50)
    await disk.check(config(), state_path, sender, 100.0, send=True)
    sender.send.assert_not_called()
    usage(monkeypatch, 81)
    await disk.check(config(), state_path, sender, 200.0, send=True)
    await disk.check(config(), state_path, sender, 201.0, send=True)
    assert sender.send.await_count == 1
    usage(monkeypatch, 91)
    await disk.check(config(), state_path, sender, 202.0, send=True)
    usage(monkeypatch, 89)
    result = await disk.check(config(), state_path, sender, 203.0, send=True)
    assert result[0].level == "CRITICAL" and sender.send.await_count == 2
    usage(monkeypatch, 84)
    await disk.check(config(), state_path, sender, 204.0, send=True)
    assert sender.send.await_count == 3
    usage(monkeypatch, 76)
    await disk.check(config(), state_path, sender, 205.0, send=True)
    assert sender.send.await_count == 3
    usage(monkeypatch, 70)
    await disk.check(config(), state_path, sender, 206.0, send=True)
    await disk.check(config(), state_path, sender, 10000.0, send=True)
    assert sender.send.await_count == 4


@pytest.mark.parametrize("enabled,send", [(False, True), (True, False), (False, False)])
async def test_delivery_requires_both_opt_ins(monkeypatch, state_path, enabled, send):
    usage(monkeypatch, 95)
    sender = AsyncMock()
    await disk.check(config(enabled=enabled), state_path, sender, 100.0, send=send)
    sender.send.assert_not_called()
    assert not state_path.exists()


@pytest.mark.parametrize("outcome", ["unknown", "interrupted"])
async def test_uncertain_or_interrupted_delivery_is_never_blindly_resent(
    monkeypatch, state_path, outcome
):
    usage(monkeypatch, 95)
    sender = AsyncMock()
    sender.send.side_effect = (
        GmailError("UNKNOWN", uncertain=True)
        if outcome == "unknown"
        else RuntimeError("interrupted")
    )
    if outcome == "interrupted":
        with pytest.raises(RuntimeError):
            await disk.check(config(), state_path, sender, 100.0, send=True)
    else:
        await disk.check(config(), state_path, sender, 100.0, send=True)
    sender.send.side_effect = None
    await disk.check(config(), state_path, sender, 100000.0, send=True)
    assert sender.send.await_count == 1
    assert "operator@example.com" not in state_path.read_text()
    assert state_path.stat().st_mode & 0o777 == 0o600


async def test_pending_intent_is_durable_before_mail(monkeypatch, state_path):
    usage(monkeypatch, 90)

    async def send(*args):
        state = disk.MonitorState.model_validate_json(disk.read_private(state_path))
        assert next(iter(state.disks["database"].deliveries.values())).status == "PENDING"
        return "provider-accepted"

    sender = AsyncMock()
    sender.send.side_effect = send
    await disk.check(config(), state_path, sender, 100.0, send=True)
    await disk.check(config(), state_path, sender, 3700.0, send=True)
    assert sender.send.await_count == 2


async def test_unknown_filesystem_is_not_normal(monkeypatch, state_path):
    def unavailable(path):
        raise OSError("private path")

    monkeypatch.setattr(os, "statvfs", unavailable)
    sender = AsyncMock()
    samples = await disk.check(config(), state_path, sender, 100.0, send=True)
    assert samples[0].level == "UNKNOWN" and samples[0].used_percent is None
    assert sender.send.await_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"recipients": []},
        {"warning_percent": 95.0},
        {"recovery_percent": 85.0},
        {"recipients": ["x@example.com", "X@example.com"]},
        {"enabled": "true"},
        {"targets": [{"name": "disk", "path": "relative"}]},
    ],
)
def test_invalid_operator_configuration(changes):
    with pytest.raises(ValidationError):
        config(**changes)


async def test_corrupt_or_public_state_blocks_email(monkeypatch, state_path):
    usage(monkeypatch, 95)
    state_path.write_text("{private@example.com")
    state_path.chmod(0o600)
    sender = AsyncMock()
    with pytest.raises(ValueError):
        await disk.check(config(), state_path, sender, 100.0, send=True)
    state_path.write_text(json.dumps({"version": 1, "disks": {}}))
    state_path.chmod(0o644)
    with pytest.raises(ValueError):
        await disk.check(config(), state_path, sender, 100.0, send=True)
    sender.send.assert_not_called()


def test_private_txt_configuration(tmp_path):
    path = tmp_path / "operator.txt"
    path.write_text(
        "[monitor]\nenabled = false\nrecipients =\nwarning_percent = 75\n"
        "critical_percent = 90\n[disk:system]\npath = /\nrequire_mount = true\n"
    )
    path.chmod(0o600)
    loaded = disk.load_config(path)
    assert loaded.warning_percent == 75 and loaded.critical_percent == 90
    assert loaded.recipients == [] and not loaded.enabled
    assert loaded.targets[0].require_mount
    path.write_text(path.read_text().replace("enabled = false", "enabled = true"))
    with pytest.raises(ValidationError):
        disk.load_config(path)


def test_missing_expected_mount_is_unknown(monkeypatch):
    usage(monkeypatch, 50)
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    target = disk.DiskTarget(name="database", path="/synthetic", require_mount=True)
    assert disk.sample(target, config(), "NORMAL").level == "UNKNOWN"


async def test_symlink_state_and_failed_state_write_prevent_send(monkeypatch, state_path):
    usage(monkeypatch, 95)
    target = state_path.parent / "target"
    target.write_text("{}")
    state_path.symlink_to(target)
    sender = AsyncMock()
    with pytest.raises(OSError):
        await disk.check(config(), state_path, sender, 100.0, send=True)
    state_path.unlink()

    def failed(*args):
        raise OSError("storage full")

    monkeypatch.setattr(disk, "write_state", failed)
    with pytest.raises(OSError):
        await disk.check(config(), state_path, sender, 100.0, send=True)
    sender.send.assert_not_called()


def test_cli_inspection_does_not_send_or_expose_addresses(monkeypatch, tmp_path, capsys):
    from src.app.cli import disk_alerts as cli

    file = tmp_path / "private.txt"
    file.write_text(
        "[monitor]\nenabled = true\nrecipients = private@example.com\n[disk:system]\npath = /\n"
    )
    file.chmod(0o600)
    state = tmp_path / "state.json"
    monkeypatch.setattr("sys.argv", ["disk_alerts", "--config", str(file), "--state", str(state)])
    send = AsyncMock()
    monkeypatch.setattr(cli.GmailSender, "send", send)
    cli.main()
    output = capsys.readouterr().out
    assert "private@example.com" not in output
    assert json.loads(output)["deliveryEnabled"] is False
    assert not state.exists()
    send.assert_not_called()


def test_editing_two_txt_thresholds_updates_derived_recovery_without_restart(tmp_path):
    path = tmp_path / "operator.txt"
    text = "[monitor]\nwarning_percent = 75\ncritical_percent = 90\n[disk:system]\npath = /\n"
    path.write_text(text)
    path.chmod(0o600)
    first = disk.load_config(path)
    assert (first.normal_threshold, first.critical_exit_threshold) == (70.0, 85.0)
    path.write_text(text.replace("75", "80").replace("90", "84"))
    second = disk.load_config(path)
    assert (second.warning_percent, second.critical_percent) == (80.0, 84.0)
    assert (second.normal_threshold, second.critical_exit_threshold) == (75.0, 82.0)


async def test_incomplete_state_cannot_reset_notification_history(monkeypatch, state_path):
    usage(monkeypatch, 95)
    state_path.write_text("{}")
    state_path.chmod(0o600)
    sender = AsyncMock()
    with pytest.raises(ValidationError):
        await disk.check(config(), state_path, sender, 100.0, send=True)
    sender.send.assert_not_called()
