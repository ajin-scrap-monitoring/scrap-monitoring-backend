"""Sensor Replay Pipeline Test Suite."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from src.app.core.config import get_settings


@pytest.mark.asyncio
async def test_lidar_sensor_replay_pipeline(client: AsyncClient) -> None:
    """Replay synthetic LiDAR frames through backend ingestion and verify persistence & state."""
    settings = get_settings()
    base_time = datetime.now(UTC) - timedelta(minutes=10)

    # 10 consecutive synthetic frames simulating filling scrap
    frames = [
        {"fill": 20.0, "height": 60.0, "exp_state": "NORMAL"},
        {"fill": 35.0, "height": 105.0, "exp_state": "NORMAL"},
        {"fill": 50.0, "height": 150.0, "exp_state": "NORMAL"},
        {"fill": 65.0, "height": 195.0, "exp_state": "NORMAL"},
        {"fill": 74.0, "height": 222.0, "exp_state": "NORMAL"},
        {"fill": 76.0, "height": 228.0, "exp_state": "WARNING"},  # Exceeds 75%
        {"fill": 80.0, "height": 240.0, "exp_state": "WARNING"},
        {"fill": 86.0, "height": 258.0, "exp_state": "CRITICAL"},  # Exceeds 85%
        {"fill": 87.0, "height": 261.0, "exp_state": "CRITICAL"},  # Still CRITICAL
        {"fill": 84.5, "height": 253.5, "exp_state": "CRITICAL"},  # Hysteresis keeps CRITICAL
    ]

    for i, frame in enumerate(frames):
        timestamp = base_time + timedelta(seconds=i * 5)
        payload = {
            "pit_id": "pit-replay",
            "measured_at": timestamp.isoformat(),
            "lidar1_distance_cm": 300.0 - frame["height"],
            "lidar2_distance_cm": 300.0 - frame["height"] + 2.0,
            "calculated_height_cm": frame["height"],
            "fill_ratio_percent": frame["fill"],
            "sensor1_status": "OK",
            "sensor2_status": "OK",
        }

        resp = await client.post(
            "/api/v1/metrics/ingest",
            json=payload,
            headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["state"] == frame["exp_state"], (
            f"Frame {i}: fill={frame['fill']}% — expected {frame['exp_state']}, got {data['state']}"
        )

    # Verify all frames are persisted in database
    history_resp = await client.get("/api/v1/metrics/history?pit_id=pit-replay&limit=20")
    assert history_resp.status_code == 200
    history_data = history_resp.json()
    assert len(history_data) == len(frames)
