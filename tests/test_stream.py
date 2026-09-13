"""Tests for real-time streaming, pit configuration, and metric stats."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from src.app.core.config import get_settings
from src.app.schemas.state import RealtimeStatePayload
from src.app.services.broadcaster import broadcaster


@pytest.mark.asyncio
async def test_pit_config_crud(client: AsyncClient) -> None:
    """Test pit configuration listing, reading, and updating."""
    # 1. List configs (creates default pit-01 if empty)
    resp = await client.get("/api/v1/config/pits")
    assert resp.status_code == 200
    pits = resp.json()
    assert len(pits) >= 1
    assert pits[0]["id"] == "pit-01"

    # 2. Get specific pit
    resp = await client.get("/api/v1/config/pits/pit-01")
    assert resp.status_code == 200
    assert resp.json()["id"] == "pit-01"

    # 3. Update pit thresholds
    update_payload = {
        "warning_threshold_percent": 80.0,
        "critical_threshold_percent": 90.0,
    }
    update_resp = await client.put("/api/v1/config/pits/pit-01", json=update_payload)
    assert update_resp.status_code == 200
    updated_data = update_resp.json()
    assert updated_data["warning_threshold_percent"] == 80.0
    assert updated_data["critical_threshold_percent"] == 90.0

    # 4. Non-existent pit returns 404
    not_found_resp = await client.get("/api/v1/config/pits/non-existent")
    assert not_found_resp.status_code == 404


@pytest.mark.asyncio
async def test_metric_stats_query(client: AsyncClient) -> None:
    """Test stats endpoint returns aggregated metric summary."""
    settings = get_settings()

    # Ingest 2 metrics
    for fill in [40.0, 60.0]:
        payload = {
            "pit_id": "pit-01",
            "measured_at": datetime.now(UTC).isoformat(),
            "calculated_height_cm": 150.0,
            "fill_ratio_percent": fill,
            "sensor1_status": "OK",
            "sensor2_status": "OK",
        }
        resp = await client.post(
            "/api/v1/metrics/ingest",
            json=payload,
            headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
        )
        assert resp.status_code == 201

    # Query stats
    stats_resp = await client.get("/api/v1/metrics/stats?pit_id=pit-01")
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats is not None
    assert stats["sample_count"] >= 2
    assert stats["min_fill_ratio"] <= 40.0
    assert stats["max_fill_ratio"] >= 60.0


@pytest.mark.asyncio
async def test_broadcaster_queue() -> None:
    """Test broadcaster registers queue and pushes state."""
    queue = await broadcaster.register_sse_queue()
    try:
        # Consume initial cached state if present
        if not queue.empty():
            await queue.get()

        sample_state = RealtimeStatePayload(
            pit_id="pit-01",
            fill_ratio_percent=72.5,
            calculated_height_cm=210.0,
            state="NORMAL",
            measured_at=datetime.now(UTC),
            warning_threshold_percent=75.0,
            critical_threshold_percent=85.0,
            sensor1_status="OK",
            sensor2_status="OK",
            is_valid=True,
        )
        await broadcaster.broadcast(sample_state)

        # Queue should receive the broadcasted message
        msg = await queue.get()
        assert "pit-01" in msg
        assert "72.5" in msg
        assert broadcaster.latest_state is not None
        assert broadcaster.latest_state.fill_ratio_percent == 72.5
    finally:
        await broadcaster.unregister_sse_queue(queue)
