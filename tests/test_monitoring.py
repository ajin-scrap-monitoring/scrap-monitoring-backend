"""Tests for monitoring dashboard snapshot and realtime event stream endpoints."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1.monitoring import stream_monitoring_events
from src.app.core.config import get_settings


@pytest.mark.asyncio
async def test_monitoring_snapshot_empty(client: AsyncClient) -> None:
    """Test snapshot endpoint when no metrics exist yet."""
    resp = await client.get("/api/v1/monitoring/snapshot?pit_id=pit-empty")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    data = resp.json()
    assert data["schemaVersion"] == "1"
    assert data["systemStatus"] == "no_data"
    assert data["operationState"] == "idle"
    assert data["summary"]["loadPercent"] == 0.0
    assert len(data["devices"]) == 4
    assert len(data["activeAlerts"]) == 0
    assert len(data["lidarProfiles"]) == 2
    assert data["lidarProfiles"][0]["unit"] == "m"


@pytest.mark.asyncio
async def test_monitoring_snapshot_with_metric(client: AsyncClient) -> None:
    """Test snapshot endpoint after normal and critical metric ingestions."""
    settings = get_settings()

    # 1. Ingest normal metric
    normal_payload = {
        "pit_id": "pit-test",
        "measured_at": datetime.now(UTC).isoformat(),
        "lidar1_distance_cm": 240.0,
        "lidar2_distance_cm": 240.0,
        "calculated_height_cm": 60.0,
        "fill_ratio_percent": 20.0,
        "sensor1_status": "OK",
        "sensor2_status": "OK",
    }
    ingest_resp = await client.post(
        "/api/v1/metrics/ingest",
        json=normal_payload,
        headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
    )
    assert ingest_resp.status_code == 201

    snap_resp = await client.get("/api/v1/monitoring/snapshot?pit_id=pit-test")
    assert snap_resp.status_code == 200
    snap = snap_resp.json()
    assert snap["systemStatus"] == "normal"
    assert snap["operationState"] == "accumulating"
    assert snap["summary"]["loadPercent"] == 20.0
    assert len(snap["activeAlerts"]) == 0
    assert len(snap["recentLoad"]) >= 1
    assert snap["devices"][0]["status"] == "normal"
    assert snap["devices"][1]["status"] == "normal"

    # 2. Ingest critical metric
    crit_payload = {
        "pit_id": "pit-test",
        "measured_at": datetime.now(UTC).isoformat(),
        "lidar1_distance_cm": 30.0,
        "lidar2_distance_cm": 30.0,
        "calculated_height_cm": 270.0,
        "fill_ratio_percent": 90.0,
        "sensor1_status": "OK",
        "sensor2_status": "OK",
    }
    await client.post(
        "/api/v1/metrics/ingest",
        json=crit_payload,
        headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
    )

    crit_snap_resp = await client.get("/api/v1/monitoring/snapshot?pit_id=pit-test")
    assert crit_snap_resp.status_code == 200
    crit_snap = crit_snap_resp.json()
    assert crit_snap["systemStatus"] == "collection_required"
    assert crit_snap["operationState"] == "collecting"
    assert crit_snap["summary"]["loadPercent"] == 90.0
    assert len(crit_snap["activeAlerts"]) == 1
    assert crit_snap["activeAlerts"][0]["type"] == "collection_required"
    assert crit_snap["activeAlerts"][0]["severity"] == "error"


@pytest.mark.asyncio
async def test_monitoring_events_sse(test_db_session: AsyncSession) -> None:
    """Test SSE endpoint response headers and initial monitoring.snapshot event."""
    response = await stream_monitoring_events(pit_id="pit-test", db=test_db_session)
    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"

    gen = response.body_iterator
    first_event = await anext(gen)
    assert first_event.startswith("event: monitoring.snapshot\n")
    assert "data: {" in first_event
    await gen.aclose()
