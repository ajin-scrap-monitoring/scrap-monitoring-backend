"""Tests for dashboard header notification center endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.config import PitConfig
from src.app.models.log import AlertLog


@pytest.fixture(autouse=True)
async def authenticated_viewer(client: AsyncClient):
    """Existing notification behavior is exercised for the logged-in account."""
    response = await client.post(
        "/api/v1/session", json={"username": "operator", "password": "operator1234"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrfToken"]


@pytest.mark.asyncio
async def test_notifications_empty(client: AsyncClient) -> None:
    """Test empty notification center query."""
    resp = await client.get("/api/v1/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["unreadCount"] == 0
    assert data["page"]["totalItems"] == 0


@pytest.mark.parametrize(
    "event_type,severity,fill,title",
    [
        ("collection_required", "warning", 96.0, "스크랩 적재율 경고 알림"),
        ("collection_required", "error", 80.0, "스크랩 수거 필요 알림 (위험 임계치 초과)"),
        ("collection_required", "info", 96.0, "스크랩 수거 관련 알림"),
        ("pre_collection_alert", "warning", 96.0, "스크랩 적재율 사전 경고 알림"),
        ("collection_started", "info", 96.0, "스크랩 수거 시작 알림"),
        ("collection_completed", "info", 96.0, "스크랩 수거 완료 알림"),
        ("measurement_error", "error", 96.0, "측정 오류 알림"),
        ("device_error", "error", 96.0, "장비 오류 알림"),
        ("video_delay", "warning", 96.0, "영상 지연 알림"),
    ],
)
async def test_notification_title_describes_recorded_event_not_fixed_percent(
    client: AsyncClient,
    test_db_session: AsyncSession,
    event_type: str,
    severity: str,
    fill: float,
    title: str,
) -> None:
    """A high fill reading must not turn a device error or warning into a critical alert."""
    log = AlertLog(
        pit_id="pit-title",
        triggered_at=datetime.now(UTC),
        fill_ratio_percent=fill,
        channel="LOG",
        status="RECORDED",
        message="Recorded synthetic event",
        event_type=event_type,
        severity=severity,
    )
    test_db_session.add(log)
    await test_db_session.commit()
    response = await client.get("/api/v1/notifications")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert (item["title"], item["type"], item["severity"]) == (title, event_type, severity)
    read = await client.patch(f"/api/v1/notifications/{item['id']}", json={"read": True})
    assert read.status_code == 200 and read.json()["title"] == title
    assert log.fill_ratio_percent == fill and log.severity == severity and log.status == "RECORDED"


async def test_notification_title_stays_historical_after_threshold_change(
    client: AsyncClient, test_db_session: AsyncSession
) -> None:
    """Changing today's thresholds must not change the title of an existing warning."""
    pit = PitConfig(
        id="pit-title",
        name="Synthetic",
        total_depth_cm=1000,
        warning_threshold_percent=75,
        critical_threshold_percent=95,
    )
    test_db_session.add_all(
        [
            pit,
            AlertLog(
                pit_id=pit.id,
                triggered_at=datetime.now(UTC),
                fill_ratio_percent=90,
                channel="LOG",
                status="RECORDED",
                message="Historical warning",
                event_type="collection_required",
                severity="warning",
            ),
        ]
    )
    await test_db_session.commit()
    before = (await client.get("/api/v1/notifications")).json()["items"][0]
    assert before["title"] == "스크랩 적재율 경고 알림"
    pit.critical_threshold_percent = 85
    await test_db_session.commit()
    after = (await client.get("/api/v1/notifications")).json()["items"][0]
    assert after["title"] == before["title"]
    assert after["severity"] == "warning" and after["detail"] == before["detail"]


@pytest.mark.asyncio
async def test_notifications_crud_and_read_all(
    client: AsyncClient,
    test_db_session: AsyncSession,
) -> None:
    """Test notification listing, single read, and read-all actions."""
    now = datetime.now(UTC)

    # 1. Seed alert logs
    log1 = AlertLog(
        pit_id="pit-01",
        triggered_at=now - timedelta(minutes=5),
        fill_ratio_percent=88.0,
        channel="LOG",
        status="SENT",
        message="스크랩 적재율 88.0% 위험 임계치 도달",
        event_type="collection_required",
        severity="error",
    )
    log2 = AlertLog(
        pit_id="pit-01",
        triggered_at=now - timedelta(minutes=2),
        fill_ratio_percent=76.0,
        channel="LOG",
        status="SENT",
        message="스크랩 적재율 76.0% 사전 경고 알림",
        event_type="collection_required",
        severity="warning",
    )
    log3 = AlertLog(
        pit_id="pit-01",
        triggered_at=now,
        fill_ratio_percent=0.0,
        channel="SMS",
        status="SENT",
        message="[테스트 알림] 관리자 담당자에게 SMS 테스트 발송",
        event_type="collection_required",
        severity="info",
    )
    test_db_session.add_all([log1, log2, log3])
    await test_db_session.commit()

    # 2. List notifications (should have 3 items, ordered latest first)
    list_resp = await client.get("/api/v1/notifications?page=1&pageSize=10")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["unreadCount"] == 3
    assert data["page"]["totalItems"] == 3
    assert len(data["items"]) == 3

    # Check latest item (log3)
    latest_item = data["items"][0]
    assert latest_item["title"] == "테스트 알림 발송"
    assert latest_item["readAt"] is None

    # Check warning item (log2)
    warn_item = data["items"][1]
    assert "경고" in warn_item["title"]

    # 3. Mark single notification as read
    target_id = latest_item["id"]
    patch_resp = await client.patch(
        f"/api/v1/notifications/{target_id}",
        json={"read": True},
    )
    assert patch_resp.status_code == 200
    patched_data = patch_resp.json()
    assert patched_data["readAt"] is not None

    # 4. Check unreadCount decreased to 2
    unread_resp = await client.get("/api/v1/notifications?unreadOnly=true")
    assert unread_resp.status_code == 200
    unread_data = unread_resp.json()
    assert unread_data["unreadCount"] == 2
    assert unread_data["page"]["totalItems"] == 2

    # 5. Patch non-existent notification returns 404
    not_found_resp = await client.patch(
        "/api/v1/notifications/notif-999999",
        json={"read": True},
    )
    assert not_found_resp.status_code == 404

    # 6. Mark all as read
    read_all_resp = await client.post("/api/v1/notifications/read-all")
    assert read_all_resp.status_code == 204

    # 7. Check unreadCount is now 0
    after_all_resp = await client.get("/api/v1/notifications")
    assert after_all_resp.status_code == 200
    assert after_all_resp.json()["unreadCount"] == 0

    unread_after = await client.get("/api/v1/notifications?unreadOnly=true")
    assert unread_after.status_code == 200
    assert unread_after.json()["page"]["totalItems"] == 0
    assert unread_after.json()["items"] == []
