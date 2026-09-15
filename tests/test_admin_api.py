"""Tests for dashboard administration and alert settings endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_alert_settings_get_and_put(client: AsyncClient) -> None:
    """Test getting and updating alert settings with ETag and If-Match."""
    # 1. Get default alert settings
    get_resp = await client.get("/api/v1/settings/alerts?pit_id=pit-01")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert "collectionThresholdPercent" in data
    assert data["collectionThresholdPercent"] == 85.0
    assert data["preCollectionAlert"]["enabled"] is True
    assert data["preCollectionAlert"]["thresholdPercent"] == 75.0
    assert data["repeatIntervalMinutes"] == 30

    etag = get_resp.headers.get("ETag")
    assert etag is not None

    # 2. Update settings with invalid If-Match (412 Precondition Failed)
    invalid_update_payload = {
        "collectionThresholdPercent": 90.0,
        "preCollectionAlert": {
            "enabled": True,
            "thresholdPercent": 80.0,
        },
        "sendDelayMinutes": 5,
        "repeatIntervalMinutes": 20,
        "maximumRepeatCount": 5,
        "recoveryNotificationEnabled": False,
        "eventTypes": ["collection_required", "device_error"],
    }
    fail_resp = await client.put(
        "/api/v1/settings/alerts?pit_id=pit-01",
        json=invalid_update_payload,
        headers={"If-Match": '"wrong-etag"'},
    )
    assert fail_resp.status_code == 412

    # 3. Update settings with valid If-Match
    success_resp = await client.put(
        "/api/v1/settings/alerts?pit_id=pit-01",
        json=invalid_update_payload,
        headers={"If-Match": etag},
    )
    assert success_resp.status_code == 200
    updated_data = success_resp.json()
    assert updated_data["collectionThresholdPercent"] == 90.0
    assert updated_data["preCollectionAlert"]["thresholdPercent"] == 80.0
    assert updated_data["sendDelayMinutes"] == 5
    assert updated_data["repeatIntervalMinutes"] == 20
    assert updated_data["maximumRepeatCount"] == 5
    assert updated_data["recoveryNotificationEnabled"] is False
    assert updated_data["eventTypes"] == ["collection_required", "device_error"]

    new_etag = success_resp.headers.get("ETag")
    assert new_etag is not None
    assert new_etag != etag

    # 4. Verify GET returns updated settings
    verify_resp = await client.get("/api/v1/settings/alerts?pit_id=pit-01")
    assert verify_resp.status_code == 200
    assert verify_resp.json()["collectionThresholdPercent"] == 90.0


@pytest.mark.asyncio
async def test_notification_recipients_crud_and_test_notification(client: AsyncClient) -> None:
    """Test notification recipients CRUD, duplication error, and test notification dispatch."""
    # 1. List recipients (empty or default)
    list_resp = await client.get("/api/v1/notification-recipients?page=1&pageSize=10")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert "items" in list_data
    assert "page" in list_data
    initial_total = list_data["page"]["totalItems"]

    # 2. Create recipient
    create_payload = {
        "name": "홍길동",
        "team": "생산1팀",
        "email": "gildong@ajin.co.kr",
        "phone": "010-1234-5678",
        "enabled": True,
        "channels": ["email", "sms"],
        "subscriptionMode": "global",
        "eventTypes": ["collection_required", "measurement_error", "device_error"],
    }
    create_resp = await client.post("/api/v1/notification-recipients", json=create_payload)
    assert create_resp.status_code == 201
    created_item = create_resp.json()
    recipient_id = created_item["id"]
    assert created_item["name"] == "홍길동"
    assert created_item["team"] == "생산1팀"
    assert created_item["email"] == "gildong@ajin.co.kr"
    assert "sms" in created_item["channels"]
    assert create_resp.headers.get("Location") == f"/api/v1/notification-recipients/{recipient_id}"
    recipient_etag = create_resp.headers.get("ETag")
    assert recipient_etag is not None

    # 3. Duplicate email check (409 Conflict)
    dup_resp = await client.post("/api/v1/notification-recipients", json=create_payload)
    assert dup_resp.status_code == 409

    # 4. List recipients reflects new item
    list2_resp = await client.get("/api/v1/notification-recipients?page=1&pageSize=10")
    assert list2_resp.status_code == 200
    assert list2_resp.json()["page"]["totalItems"] == initial_total + 1

    # 5. Patch recipient
    patch_payload = {
        "team": "생산2팀",
        "enabled": False,
    }
    patch_resp = await client.patch(
        f"/api/v1/notification-recipients/{recipient_id}",
        json=patch_payload,
    )
    assert patch_resp.status_code == 200
    patched_item = patch_resp.json()
    assert patched_item["team"] == "생산2팀"
    assert patched_item["enabled"] is False
    assert patched_item["name"] == "홍길동"  # preserved

    # 6. Patch non-existent recipient returns 404
    patch_404 = await client.patch(
        "/api/v1/notification-recipients/non-existent-id",
        json={"team": "품질팀"},
    )
    assert patch_404.status_code == 404

    # 7. Test notification dispatch (202 Accepted)
    test_notif_payload = {
        "recipientId": recipient_id,
        "channels": ["sms"],
    }
    test_resp = await client.post("/api/v1/notifications/test", json=test_notif_payload)
    assert test_resp.status_code == 202
    test_data = test_resp.json()
    assert test_data["status"] == "accepted"

    # 8. Test notification to non-existent recipient (404 Not Found)
    test_404_resp = await client.post(
        "/api/v1/notifications/test",
        json={
            "recipientId": "non-existent-recipient",
            "channels": ["email"],
        },
    )
    assert test_404_resp.status_code == 404
