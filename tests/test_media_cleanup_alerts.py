"""Synthetic status, durable notification and failure recovery without real mail or deletion."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from src.app.core.config import Settings
from src.app.operations import media_cleanup_alerts as cleanup
from src.app.schemas.daily_recording import DailyRecordingMetadata
from src.app.services.gmail_sender import GmailError
from src.app.services.media_client import MediaClient
from tests.test_daily_recordings import recording
from tests.test_disk_alerts import config
from tests.test_disk_alerts import state_path as state_path


def payload(now=1000.0, percent=50, *, latched=False, **changes):
    timestamp = datetime.fromtimestamp(now, UTC)
    return {
        "enabled": True,
        "configuration_error": None,
        "active": percent >= 90,
        "mode": "active" if percent >= 90 else "idle",
        "observed_at": timestamp.isoformat(),
        "valid_until_at": (timestamp + timedelta(seconds=70)).isoformat(),
        "state_fresh": True,
        "measurement": {
            "pressures": {
                name: {
                    "used_bytes": percent * 10,
                    "limit_bytes": 1000,
                    "percent": float(percent),
                    "shortfall_bytes": max(0, percent * 10 - 1000),
                }
                for name in ("daily_budget", "minimum_free")
            }
        },
        "policy": {
            "enabled": True,
            "stop_percent": 85,
            "start_percent": 90,
            "critical_percent": 95,
            "interval_seconds": 30,
            "notification_seconds": 900,
        },
        "critical": percent >= 95,
        "alert_latched": latched,
        "notification_sequence": 0,
        "reason_code": None,
        "stalled": False,
        "attempts": 0,
        "successful_days": 0,
        "observed_reclaimed_bytes": 0,
        "last_attempt": None,
        "scope": "closed_ready_daily_mp4",
        "minimum_retention_guaranteed": False,
        **changes,
    }


def status(*args, **kwargs):
    return cleanup.CleanupStatus.model_validate_json(json.dumps(payload(*args, **kwargs)))


async def test_first_normal_and_passive_observer_do_not_send_or_consume_state(state_path):
    sender = AsyncMock()
    await cleanup.check_cleanup(config(), state_path, sender, 1000, status(), send=True)
    sender.send.assert_not_called()
    saved = state_path.read_bytes()
    await cleanup.check_cleanup(config(), state_path, sender, 1001, status(1001, 99), send=False)
    await cleanup.check_cleanup(
        config(enabled=False), state_path, sender, 1001, status(1001, 99), send=True
    )
    assert saved == state_path.read_bytes()
    sender.send.assert_not_called()


async def test_critical_repeat_restart_latch_and_exactly_one_recovery(state_path):
    sender = AsyncMock()
    await cleanup.check_cleanup(
        config(), state_path, sender, 1000, status(1000, 95, latched=True), send=True
    )
    await cleanup.check_cleanup(
        config(), state_path, sender, 1001, status(1001, 96, latched=True), send=True
    )
    assert sender.send.await_count == 1
    await cleanup.check_cleanup(
        config(), state_path, sender, 1900, status(1900, 92, latched=True), send=True
    )
    assert sender.send.await_count == 2
    # A lost upstream latch does not invent recovery above the stop threshold.
    await cleanup.check_cleanup(config(), state_path, sender, 1901, status(1901, 86), send=True)
    assert sender.send.await_count == 2
    await cleanup.check_cleanup(config(), state_path, sender, 1902, status(1902, 85), send=True)
    await cleanup.check_cleanup(config(), state_path, sender, 2902, status(2902, 50), send=True)
    assert sender.send.await_count == 3
    assert "RECOVERED" in sender.send.call_args.args[1]


async def test_stale_query_does_not_clear_critical_and_status_recovery_is_separate(state_path):
    sender = AsyncMock()
    await cleanup.check_cleanup(
        config(), state_path, sender, 1000, status(1000, 96, latched=True), send=True
    )
    await cleanup.check_cleanup(config(), state_path, sender, 1100, status(1000, 50), send=True)
    assert sender.send.await_count == 2
    state = cleanup.CleanupMailState.model_validate_json(state_path.read_bytes())
    assert state.channels["capacity"].active and state.channels["status"].active
    await cleanup.check_cleanup(
        config(), state_path, sender, 1101, status(1101, 96, latched=True), send=True
    )
    assert sender.send.await_count == 3
    assert "/status: RECOVERED" in sender.send.call_args.args[1]
    assert "공간 회복을 뜻하지 않습니다" in sender.send.call_args.args[2]


@pytest.mark.parametrize(
    "reason",
    [
        "TARGETS_EXHAUSTED",
        "PROTECTED_TARGETS_ONLY",
        "TARGET_EXCEEDS_LIMIT",
        "DELETE_ERROR",
        "SPACE_NOT_RECLAIMED",
        "GENERATION_OUTPACES_CLEANUP",
    ],
)
async def test_blocked_or_failed_cleanup_is_reported_below_critical(state_path, reason):
    sender = AsyncMock()
    await cleanup.check_cleanup(
        config(),
        state_path,
        sender,
        1000,
        status(1000, 91, mode="blocked", reason_code=reason),
        send=True,
    )
    assert sender.send.await_count == 1 and "/cleanup: ALERT" in sender.send.call_args.args[1]
    assert reason in sender.send.call_args.args[2]


async def test_stalled_and_successful_files_do_not_claim_space_recovery(state_path):
    sender = AsyncMock()
    value = status(
        1000,
        91,
        stalled=True,
        successful_days=1,
        last_attempt={"success": True, "space_recovery_confirmed": False},
    )
    await cleanup.check_cleanup(config(), state_path, sender, 1000, value, send=True)
    assert sender.send.await_count == 1
    assert "최근 시도 공간 회수 확인: False" in sender.send.call_args.args[2]


@pytest.mark.parametrize(
    "change",
    [
        {"state_fresh": False},
        {"enabled": False},
        {"configuration_error": "INVALID_CAPACITY_CLEANUP_CONFIG"},
        {"measurement": None},
        {"critical": True},
        {"observed_at": "1970-01-01T00:20:00Z"},
        {"valid_until_at": "1970-01-02T00:00:00Z"},
        {"observed_at": "1970-01-01T09:16:40+09:00"},
    ],
)
def test_invalid_or_unfresh_status_never_means_normal(change):
    value = status(**change)
    result = cleanup.notices(value, cleanup.CleanupMailState(), 1000, 3600)
    assert len(result) == 1 and result[0].channel == "status" and result[0].active


@pytest.mark.parametrize(
    "changes",
    [
        {"critical": "false"},
        {"attempts": True},
        {"notification_sequence": -1},
        {"mode": "unknown-mode"},
        {"scope": "everything"},
        {"minimum_retention_guaranteed": True},
        {"reason_code": "private value with spaces"},
    ],
)
def test_bad_status_types_or_unknown_contract_rejected(changes):
    with pytest.raises(ValidationError):
        status(**changes)


@pytest.mark.parametrize("outcome", ["uncertain", "interrupted", "rejected"])
async def test_durable_intent_and_provider_uncertainty(state_path, outcome):
    sender = AsyncMock()

    async def fail(*args):
        saved = cleanup.CleanupMailState.model_validate_json(state_path.read_bytes())
        assert next(iter(saved.channels["capacity"].deliveries.values())).status == "PENDING"
        if outcome == "interrupted":
            raise RuntimeError("interrupted")
        raise GmailError(
            "UNKNOWN" if outcome == "uncertain" else "REJECTED", uncertain=outcome == "uncertain"
        )

    sender.send.side_effect = fail
    if outcome == "interrupted":
        with pytest.raises(RuntimeError):
            await cleanup.check_cleanup(
                config(), state_path, sender, 1000, status(1000, 96), send=True
            )
    else:
        await cleanup.check_cleanup(config(), state_path, sender, 1000, status(1000, 96), send=True)
    sender.send.side_effect = None
    await cleanup.check_cleanup(config(), state_path, sender, 2000, status(2000, 96), send=True)
    assert sender.send.await_count == (2 if outcome == "rejected" else 1)
    assert "operator@example.com" not in state_path.read_text()
    assert state_path.stat().st_mode & 0o777 == 0o600


async def test_separate_state_never_modifies_disk_monitor_state(state_path):
    disk_state = state_path.parent / "existing-disk.json"
    disk_state.write_text('{"version":1,"disks":{}}')
    before = disk_state.read_bytes()
    await cleanup.check_cleanup(
        config(), state_path, AsyncMock(), 1000, status(1000, 96), send=True
    )
    assert disk_state.read_bytes() == before


@pytest.mark.parametrize(
    "kind", ["large_length", "large_body", "redirect", "error", "bad_json", "cache"]
)
async def test_status_client_rejects_bad_bounded_response(monkeypatch, kind):
    monkeypatch.setattr(
        "src.app.services.media_client.get_settings",
        lambda: Settings(MEDIA_DAILY_SERVICE_URL="http://media.test/api/v2"),
    )
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/v2/capacity-cleanup/status"
        assert not request.content and not request.url.query
        if kind == "large_length":
            return httpx.Response(200, headers={"Content-Length": "65537"})
        if kind == "large_body":
            return httpx.Response(200, content=b"x" * 65537)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "http://private.invalid"})
        if kind == "error":
            return httpx.Response(
                503, json={"error": {"code": "SERVICE_BUSY", "message": "private"}}
            )
        if kind == "bad_json":
            return httpx.Response(200, content=b"{", headers={"Cache-Control": "no-store"})
        return httpx.Response(200, json=payload())

    client = MediaClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await cleanup.observe(client) is None
        assert len(requests) == 1
    finally:
        await client.close()


async def test_status_client_get_only_and_optional_deletion_reason(monkeypatch):
    monkeypatch.setattr(
        "src.app.services.media_client.get_settings",
        lambda: Settings(MEDIA_DAILY_SERVICE_URL="http://media.test/api/v2"),
    )

    def handle(request):
        assert request.method == "GET" and request.url.path.endswith("/capacity-cleanup/status")
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, json=payload(), headers={"Cache-Control": "no-store"})

    client = MediaClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert (await cleanup.fetch_status(client)).usable(datetime.fromtimestamp(1000, UTC))
    finally:
        await client.close()
    original = recording()
    old = DailyRecordingMetadata.model_validate(original)
    assert "deletion_reason" not in old.model_dump(mode="json")
    original.update(status="deleted", deletion_reason="capacity_pressure")
    new = DailyRecordingMetadata.model_validate(original)
    assert new.model_dump(mode="json")["deletion_reason"] == "capacity_pressure"
    assert new.status == "deleted" and new.has_gaps == old.has_gaps


def test_pressure_consistency_and_global_cap_are_not_relaxed():
    bad = payload()
    bad["measurement"]["pressures"]["daily_budget"]["percent"] = 0.0
    with pytest.raises(ValidationError):
        cleanup.CleanupStatus.model_validate_json(json.dumps(bad))


async def test_recovery_requires_all_limits_and_preserves_unavailable_cleanup(state_path):
    sender = AsyncMock()
    await cleanup.check_cleanup(
        config(), state_path, sender, 1000, status(1000, 96, latched=True, stalled=True), send=True
    )
    assert sender.send.await_count == 2
    await cleanup.check_cleanup(config(), state_path, sender, 1100, None, send=True)
    saved = cleanup.CleanupMailState.model_validate_json(state_path.read_bytes())
    assert saved.channels["cleanup"].active and saved.channels["capacity"].active
    value = status(1101, 80, stalled=True, active=True, mode="active")
    value.measurement.pressures["minimum_free"] = cleanup.Pressure(
        used_bytes=900, limit_bytes=1000, percent=90.0, shortfall_bytes=0
    )
    before = sender.send.await_count
    await cleanup.check_cleanup(config(), state_path, sender, 1101, value, send=True)
    assert sender.send.await_count == before + 1  # Query recovery only; no capacity recovery.
    saved = cleanup.CleanupMailState.model_validate_json(state_path.read_bytes())
    assert saved.channels["capacity"].active


async def test_state_write_failure_prevents_mail(state_path, monkeypatch):
    sender = AsyncMock()

    def fail(*args):
        raise OSError("state unavailable")

    monkeypatch.setattr(cleanup, "write_state", fail)
    with pytest.raises(OSError):
        await cleanup.check_cleanup(config(), state_path, sender, 1000, status(1000, 96), send=True)
    sender.send.assert_not_called()


async def test_custom_limit_cannot_relax_global_cap():
    with pytest.raises(ValueError):
        await MediaClient()._request(
            "GET", "capacity-cleanup/status", (200,), daily=True, max_bytes=8388609
        )


async def test_new_recipient_does_not_receive_recovery_of_someone_elses_alert(state_path):
    sender = AsyncMock()
    await cleanup.check_cleanup(config(), state_path, sender, 1000, status(1000, 96), send=True)
    await cleanup.check_cleanup(config(), state_path, sender, 1001, status(1001, 50), send=True)
    assert sender.send.await_count == 2
    changed = config(recipients=["operator@example.com", "new@example.com"])
    await cleanup.check_cleanup(changed, state_path, sender, 1002, status(1002, 50), send=True)
    assert sender.send.await_count == 2
    await cleanup.check_cleanup(changed, state_path, sender, 1003, status(1003, 96), send=True)
    assert sender.send.await_count == 4
