"""Tests for dashboard administration and alert settings endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.models.recipient import NotificationRecipientModel
from src.app.schemas.admin import NotificationRecipientPatch
from src.app.services.admin_service import admin_service


@pytest.fixture(autouse=True)
async def authenticated_admin(client: AsyncClient):
    """Exercise existing business behavior through an authorized browser session."""
    response = await client.post(
        "/api/v1/session", json={"username": "admin", "password": "admin1234"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrfToken"]


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


@pytest.mark.asyncio
async def test_recipient_rejects_stale_etag_without_overwriting(client: AsyncClient) -> None:
    """Two open dashboard forms cannot overwrite a newer saved preference."""
    created = await client.post(
        "/api/v1/notification-recipients",
        json={
            "name": "Synthetic recipient",
            "team": "Test",
            "email": "recipient-etag@example.com",
            "channels": ["email"],
            "eventTypes": ["collection_required"],
        },
    )
    assert created.status_code == 201
    path = f"/api/v1/notification-recipients/{created.json()['id']}"
    version = created.headers["ETag"]
    saved = await client.patch(path, json={"enabled": False}, headers={"If-Match": version})
    assert saved.status_code == 200
    assert saved.headers["ETag"] != version
    assert saved.json()["version"] == saved.headers["ETag"]

    for stale in (version, '"unknown"', "W/" + saved.headers["ETag"]):
        rejected = await client.patch(
            path, json={"enabled": True, "channels": ["sms"]}, headers={"If-Match": stale}
        )
        assert rejected.status_code == 412
    items = (await client.get("/api/v1/notification-recipients")).json()["items"]
    row = next(item for item in items if item["id"] == created.json()["id"])
    assert row["enabled"] is False
    assert row["channels"] == ["email"]
    assert row["version"] == saved.headers["ETag"]
    current = await client.patch(
        path, json={"channels": ["sms"]}, headers={"If-Match": row["version"]}
    )
    assert current.status_code == 200
    assert current.json()["channels"] == ["sms"]


@pytest.mark.asyncio
async def test_recipient_phone_clear_is_persisted_and_omission_preserves_it(
    client: AsyncClient, test_engine
) -> None:
    """PATCH distinguishes clearing the phone from editing an unrelated field."""
    created = await client.post(
        "/api/v1/notification-recipients",
        json={
            "name": "Synthetic phone recipient",
            "team": "Test",
            "email": "phone-clear@example.com",
            "phone": "000-0000-0000",
            "channels": ["email"],
            "eventTypes": ["collection_required"],
        },
    )
    assert created.status_code == 201
    identifier = created.json()["id"]
    path = f"/api/v1/notification-recipients/{identifier}"
    original_etag = created.headers["ETag"]
    edited = await client.patch(path, json={"team": "Updated"}, headers={"If-Match": original_etag})
    assert edited.status_code == 200
    assert edited.json()["phone"] == "000-0000-0000"
    stale = await client.patch(path, json={"phone": None}, headers={"If-Match": original_etag})
    assert stale.status_code == 412
    cleared = await client.patch(
        path, json={"phone": None}, headers={"If-Match": edited.headers["ETag"]}
    )
    assert cleared.status_code == 200
    assert cleared.json()["phone"] is None
    assert cleared.json()["team"] == "Updated"
    assert cleared.headers["ETag"] != edited.headers["ETag"]
    async with async_sessionmaker(test_engine)() as db:
        record = await db.get(NotificationRecipientModel, identifier)
        assert record is not None and record.phone is None
    listed = (await client.get("/api/v1/notification-recipients")).json()["items"]
    assert next(item for item in listed if item["id"] == identifier)["phone"] is None


@pytest.mark.asyncio
async def test_invalid_recipient_patch_does_not_change_saved_preferences(
    client: AsyncClient,
) -> None:
    """Invalid partial forms cannot silently erase required recipient preferences."""
    created = await client.post(
        "/api/v1/notification-recipients",
        json={
            "name": "Synthetic validation recipient",
            "team": "Test",
            "email": "patch-validation@example.com",
            "channels": ["email"],
            "eventTypes": ["collection_required"],
        },
    )
    assert created.status_code == 201
    identifier = created.json()["id"]
    path = f"/api/v1/notification-recipients/{identifier}"
    invalid = [
        {},
        {"unknown": "value"},
        {"team": "Changed", "unknown": "value"},
        {"name": ""},
        {"team": ""},
        {"channels": []},
        {"channels": ["email", "email"]},
        {"eventTypes": ["collection_required", "collection_required"]},
        *(
            {field: None}
            for field in (
                "name",
                "team",
                "email",
                "enabled",
                "channels",
                "subscriptionMode",
                "eventTypes",
            )
        ),
    ]
    for payload in invalid:
        rejected = await client.patch(
            path, json=payload, headers={"If-Match": created.headers["ETag"]}
        )
        assert rejected.status_code == 422, payload
    listed = (await client.get("/api/v1/notification-recipients")).json()["items"]
    assert next(item for item in listed if item["id"] == identifier) == created.json()
    # An empty custom event selection and false are valid values, not omissions.
    valid = await client.patch(
        path,
        json={"enabled": False, "subscriptionMode": "custom", "eventTypes": []},
        headers={"If-Match": created.headers["ETag"]},
    )
    assert valid.status_code == 200
    assert valid.json()["enabled"] is False
    assert valid.json()["eventTypes"] == []


@pytest.mark.asyncio
async def test_recipient_rejects_version_change_during_save(test_engine, monkeypatch) -> None:
    """A concurrent commit after the read still wins over an in-flight stale save."""
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy.sql import Select

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as seed:
        seed.add(
            NotificationRecipientModel(
                id="synthetic-race",
                name="Test",
                team="Test",
                email="race@example.com",
                version_id=1,
                enabled=True,
                channels="email",
            )
        )
        await seed.commit()
    read_finished, continue_save = asyncio.Event(), asyncio.Event()
    async with factory() as slow, factory() as fast:
        original_execute = slow.execute

        async def paused_execute(statement, *args, **kwargs):
            result = await original_execute(statement, *args, **kwargs)
            if isinstance(statement, Select):
                # Materialize the first reader's snapshot before the other administrator saves.
                result = result.freeze()()
                read_finished.set()
                await continue_save.wait()
            return result

        monkeypatch.setattr(slow, "execute", paused_execute)
        pending = asyncio.create_task(
            admin_service.update_recipient(
                "synthetic-race",
                NotificationRecipientPatch(enabled=True, channels=["sms"]),
                slow,
                if_match='"1"',
            )
        )
        try:
            await asyncio.wait_for(read_finished.wait(), timeout=5)
            saved = await admin_service.update_recipient(
                "synthetic-race",
                NotificationRecipientPatch(enabled=False),
                fast,
                if_match='"1"',
            )
            assert saved is not None and saved[1] == '"2"'
        finally:
            continue_save.set()
        with pytest.raises(HTTPException) as rejected:
            await asyncio.wait_for(pending, timeout=5)
        assert rejected.value.status_code == 412
    async with factory() as check:
        row = await check.scalar(
            select(NotificationRecipientModel).where(
                NotificationRecipientModel.id == "synthetic-race"
            )
        )
        assert row is not None
        assert (row.enabled, row.channels, row.version_id) == (False, "email", 2)


@pytest.mark.asyncio
async def test_record_only_test_notification_does_not_claim_delivery(
    client: AsyncClient, test_db_session
) -> None:
    """A dashboard test request records an informational notice, not a delivery receipt."""
    from src.app.models.log import AlertLog

    created = await client.post(
        "/api/v1/notification-recipients",
        json={
            "name": "Synthetic test recipient",
            "team": "Test",
            "email": "recording@example.com",
            "phone": "000-0000-0000",
            "channels": ["email", "sms"],
            "eventTypes": ["collection_required"],
        },
    )
    assert created.status_code == 201
    response = await client.post(
        "/api/v1/notifications/test",
        json={"recipientId": created.json()["id"], "channels": ["email", "sms"]},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    row = await test_db_session.scalar(select(AlertLog))
    assert row is not None
    assert row.status == "RECORDED"
    assert row.channel == "EMAIL,SMS"
    assert row.severity == "info"
    assert "전송 완료" not in row.message
    assert "실제 발송은 수행하지 않았습니다" in row.message

    listed = await client.get("/api/v1/notifications")
    assert listed.status_code == 200
    notice = listed.json()["items"][0]
    assert notice["title"] == "테스트 알림 요청 기록"
    assert notice["detail"] == row.message
    assert notice["severity"] == "info"
    assert listed.json()["unreadCount"] == 1
    read = await client.patch(f"/api/v1/notifications/{notice['id']}", json={"read": True})
    assert read.status_code == 200
    assert read.json()["readAt"] is not None
    assert (await client.get("/api/v1/notifications")).json()["unreadCount"] == 0
    await test_db_session.refresh(row)
    assert row.status == "RECORDED"
