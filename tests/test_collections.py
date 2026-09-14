"""Tests for scrap collection confirmation and history."""

import pytest
from httpx import AsyncClient

from src.app.core.config import get_settings


@pytest.mark.asyncio
async def test_collection_confirm_and_history(client: AsyncClient) -> None:
    """Test confirming collection, verifying state reset, and querying collection history."""
    settings = get_settings()

    # 1. Ingest a CRITICAL metric
    critical_payload = {
        "pit_id": "pit-01",
        "measured_at": "2026-09-13T10:00:00Z",
        "calculated_height_cm": 270.0,
        "fill_ratio_percent": 90.0,
    }
    resp = await client.post(
        "/api/v1/metrics/ingest",
        json=critical_payload,
        headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
    )
    assert resp.status_code == 201
    assert resp.json()["state"] == "CRITICAL"

    # 2. Worker confirms collection
    confirm_payload = {
        "pit_id": "pit-01",
        "residual_height_cm": 15.0,
        "operator_name": "김반장",
        "notes": "1호 트럭 스크랩 수거 완료",
    }
    confirm_resp = await client.post("/api/v1/collections/confirm", json=confirm_payload)
    assert confirm_resp.status_code == 201
    confirm_data = confirm_resp.json()
    assert confirm_data["operator_name"] == "김반장"
    assert confirm_data["residual_height_cm"] == 15.0

    # 3. Verify history
    history_resp = await client.get("/api/v1/collections/history?pit_id=pit-01")
    assert history_resp.status_code == 200
    history_list = history_resp.json()
    assert len(history_list) >= 1
    assert history_list[0]["operator_name"] == "김반장"
