"""Tests for metric ingestion and query endpoints."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from src.app.core.config import get_settings


@pytest.mark.asyncio
async def test_metric_ingest_auth_failure(client: AsyncClient) -> None:
    """Test 401 Unauthorized when X-Edge-API-Key header is missing or incorrect."""
    payload = {
        "pit_id": "pit-01",
        "measured_at": datetime.now(UTC).isoformat(),
        "calculated_height_cm": 150.0,
        "fill_ratio_percent": 50.0,
    }
    # No header
    resp = await client.post("/api/v1/metrics/ingest", json=payload)
    assert resp.status_code == 401

    # Wrong key
    resp = await client.post(
        "/api/v1/metrics/ingest",
        json=payload,
        headers={"X-Edge-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_metric_ingest_and_query_success(client: AsyncClient) -> None:
    """Test successful metric ingestion, state evaluation, and retrieval."""
    settings = get_settings()
    now_str = datetime.now(UTC).isoformat()

    payload = {
        "pit_id": "pit-01",
        "measured_at": now_str,
        "lidar1_distance_cm": 120.5,
        "lidar2_distance_cm": 122.0,
        "calculated_height_cm": 180.0,
        "fill_ratio_percent": 60.0,
        "sensor1_status": "OK",
        "sensor2_status": "OK",
    }

    # Ingest
    resp = await client.post(
        "/api/v1/metrics/ingest",
        json=payload,
        headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["pit_id"] == "pit-01"
    assert data["fill_ratio_percent"] == 60.0
    assert data["state"] == "NORMAL"
    assert data["is_valid"] is True

    # Query latest
    latest_resp = await client.get("/api/v1/metrics/latest?pit_id=pit-01")
    assert latest_resp.status_code == 200
    latest_data = latest_resp.json()
    assert latest_data is not None
    assert latest_data["id"] == data["id"]
    assert latest_data["fill_ratio_percent"] == 60.0

    # Query history
    history_resp = await client.get("/api/v1/metrics/history?pit_id=pit-01")
    assert history_resp.status_code == 200
    history_data = history_resp.json()
    assert len(history_data) >= 1
    assert history_data[0]["id"] == data["id"]
