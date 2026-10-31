"""Forward cleanup evidence without converting logical deletion into physical reclamation."""

import httpx
import pytest
from test_daily_recordings import BASE, END, RID, login
from test_daily_recordings import camera as camera

from src.app.services.media_client import media_client

FIELDS = {"deleted_at", "cleanup_state", "cleanup_completed_at", "cleanup_error", "space_reclaimed"}
DELETED = "2026-10-15T00:10:00+09:00"
COMPLETED = "2026-10-15T00:11:00+09:00"


def tombstone(camera, status, state):
    camera.item.update(
        status=status,
        playback_state="unavailable",
        playback_path=None,
        export_allowed=False,
        export_state="ready",
        download_path=None,
        size_bytes=None,
        expires_at=END,
        export_expires_at=None,
        deleted_at=DELETED if status == "deleted" else None,
        cleanup_state=state,
        cleanup_completed_at=COMPLETED if state == "complete" else None,
        cleanup_error={"code": "STORAGE_ERROR", "message": "private storage path and secret"}
        if state == "failed"
        else None,
        space_reclaimed=None,
    )


@pytest.mark.parametrize("status", ["deleted", "expired"])
@pytest.mark.parametrize("state", ["pending", "failed", "complete"])
async def test_cleanup_evidence_and_historical_export_values_reach_list_and_detail(
    client, camera, status, state
):
    tombstone(camera, status, state)
    await login(client)
    response = await client.get(BASE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    item = response.json()["items"][0]
    assert item["status"] == status and item["cleanup_state"] == state
    assert item["deleted_at"] == ("2026-10-14T15:10:00Z" if status == "deleted" else None)
    assert item["cleanup_completed_at"] == ("2026-10-14T15:11:00Z" if state == "complete" else None)
    assert item["space_reclaimed"] is None
    assert item["export_state"] == "ready" and item["expires_at"] == "2026-10-14T15:00:00Z"
    assert item["download_path"] is None and item["export_allowed"] is False
    assert item["cleanup_error"] == (
        {"code": "STORAGE_ERROR", "message": "Recording cleanup failed"}
        if state == "failed"
        else None
    )
    detail = await client.get(f"{BASE}/{RID}")
    assert detail.status_code == 200 and detail.json() == item
    assert "private storage" not in response.text + detail.text
    assert all(r.method == "GET" for r in camera.requests)


async def test_legacy_missing_cleanup_fields_remain_unknown(client, camera):
    await login(client)
    body = (await client.get(f"{BASE}/{RID}")).json()
    assert all(body[field] is None for field in FIELDS)
    assert body["status"] == "recording" and body["playback_state"] == "ready"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cleanup_state", "done"),
        ("deleted_at", "2026-10-14T15:10:00"),
        ("cleanup_completed_at", "invalid"),
        ("space_reclaimed", True),
    ],
)
async def test_invalid_cleanup_evidence_is_not_reported_as_success(client, camera, field, value):
    await login(client)
    camera.item[field] = value
    response = await client.get(f"{BASE}/{RID}")
    assert response.status_code == 502 and response.json()["error"]["code"] == "MEDIA_BAD_RESPONSE"


async def test_failed_delete_keeps_503_then_explicit_retry_returns_204(client, camera, monkeypatch):
    camera.item.update(status="closed")
    attempts = 0

    async def handle(request):
        nonlocal attempts
        if request.method == "DELETE":
            attempts += 1
            tombstone(camera, "deleted", "failed" if attempts == 1 else "complete")
            if attempts == 1:
                return httpx.Response(
                    503,
                    json={"error": {"code": "STORAGE_UNAVAILABLE", "message": "private error"}},
                )
            return httpx.Response(204)
        return await camera.handle(request)

    headers = await login(client, "admin")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr(media_client, "_client", upstream)
        path = f"{BASE}/{RID}"
        failed = await client.delete(path, headers=headers)
        assert failed.status_code == 503 and "private error" not in failed.text
        after = await client.get(path)
        assert after.status_code == 200 and after.json()["cleanup_state"] == "failed"
        assert attempts == 1  # Reading a failed cleanup never retries the deletion.
        retried = await client.delete(path, headers=headers)
        assert retried.status_code == 204 and not retried.content
        assert attempts == 2
        complete = await client.get(path)
        assert complete.status_code == 200 and complete.json()["cleanup_state"] == "complete"
        assert complete.json()["space_reclaimed"] is None


def test_cleanup_fields_are_optional_and_physical_space_is_unknown(client):
    schema = client._transport.app.openapi()["components"]["schemas"]["DailyRecording"]
    assert FIELDS <= set(schema["properties"])
    assert not FIELDS & set(schema["required"])
    assert schema["properties"]["space_reclaimed"]["type"] == "null"
