"""New extraction hints follow unavailable replay evidence; existing exports stay separate."""

from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_daily_recordings import login
from test_event_recordings import API, BASE, cycle, day
from test_event_recordings import archive as archive

from src.app.core.config import get_settings
from src.app.models.media_job import EventExportAlias, MediaClipJob


@pytest.fixture
def export_enabled(archive, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://media.test/api/v1")
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", SecretStr("synthetic-export-token"))
    return archive


@pytest.mark.parametrize("mode", ["absent", "storage_full", "deleted", "expired", "conflict"])
async def test_unavailable_replay_has_no_new_export_path(
    client, test_db_session, export_enabled, mode
):
    archive = export_enabled
    end = BASE + timedelta(seconds=60)
    identifier = await cycle(test_db_session, end=end)
    if mode != "absent":
        item = day([(BASE, end, 0, 60)])
        if mode == "storage_full":
            item = day([], [(BASE, end, "STORAGE_FULL")])
            item["error"] = {"code": "INSUFFICIENT_STORAGE", "message": "private-secret"}
        elif mode == "deleted":
            item.update(status="deleted", playback_path=None)
        elif mode == "expired":
            item["expires_at"] = (BASE - timedelta(seconds=1)).isoformat()
        elif mode == "conflict":
            item["gaps"] = [
                {
                    "start_at": BASE.isoformat(),
                    "end_at": end.isoformat(),
                    "reason_code": "CAPTURE_LOSS",
                }
            ]
        archive["days"] = [item]
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["state"] == "UNAVAILABLE" and body["parts"] == []
    assert body["exportPath"] is None and body["downloadPath"] is None
    assert body["downloadReasonCode"] == "EVENT_RECORDING_UNAVAILABLE"
    assert body["reasonCodes"] and "private-secret" not in response.text
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0
    assert await test_db_session.scalar(select(func.count()).select_from(EventExportAlias)) == 0
    assert all(r.method == "GET" for r in archive["requests"])


@pytest.mark.parametrize("mode", ["complete", "partial", "pending", "hls_pending"])
async def test_existing_available_partial_and_preparing_contracts_are_retained(
    client, test_db_session, export_enabled, mode
):
    archive = export_enabled
    end = BASE + timedelta(seconds=60)
    identifier = await cycle(test_db_session, end=end)
    item = day([(BASE, end, 0, 60)])
    expected = "AVAILABLE"
    if mode == "partial":
        item = day(
            [(BASE, BASE + timedelta(seconds=20), 0, 20)],
            [(BASE + timedelta(seconds=20), end, "CAPTURE_LOSS")],
        )
        expected = "PARTIAL"
    elif mode == "pending":
        item = day([], now=BASE)
        expected = "PROCESSING"
    elif mode == "hls_pending":
        item.update(playback_state="pending", playback_path=None, playable_until_at=None)
        expected = "PROCESSING"
    archive["days"] = [item]
    await login(client)
    body = (await client.get(f"{API}/{identifier}")).json()
    assert body["state"] == expected
    assert body["exportPath"] == f"{API}/{identifier}/exports"
    assert body["downloadReasonCode"] == "EVENT_EXPORT_NOT_REQUESTED"
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("unknown", "EVENT_WINDOW_NOT_CLOSED"),
        ("too_long", "EVENT_EXPORT_RANGE_TOO_LONG"),
        ("unsupported", "EVENT_RANGE_EXPORT_UNSUPPORTED"),
    ],
)
async def test_existing_window_and_capability_reasons_keep_priority(
    client, test_db_session, export_enabled, monkeypatch, mode, reason
):
    end = None if mode == "unknown" else BASE + timedelta(hours=2 if mode == "too_long" else 1)
    identifier = await cycle(test_db_session, end=end)
    if mode == "unsupported":
        monkeypatch.setattr(get_settings(), "MEDIA_API_TOKEN", None)
    await login(client)
    response = await client.get(f"{API}/{identifier}")
    assert response.status_code == 200
    assert response.json()["exportPath"] is None
    assert response.json()["downloadReasonCode"] == reason


async def test_unavailable_details_still_require_authentication(
    client, test_db_session, export_enabled
):
    identifier = await cycle(test_db_session, end=BASE + timedelta(seconds=60))
    assert (await client.get(f"{API}/{identifier}")).status_code == 401
    assert export_enabled["requests"] == []
