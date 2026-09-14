"""Tests for media recording webhook and session verification."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_media_recording_webhook_and_query(client: AsyncClient) -> None:
    """Test media recording metadata webhook and querying indexed segments."""
    payload = {
        "segment_id": "REC-20260913-001",
        "pit_id": "pit-01",
        "start_time": "2026-09-13T09:00:00Z",
        "end_time": "2026-09-13T10:00:00Z",
        "file_path": "/srv/scrap-monitoring/media/pit-01/rec-001.mp4",
        "file_size_bytes": 104857600,
        "duration_seconds": 3600.0,
    }

    resp = await client.post("/api/v1/media/recordings/complete", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["segment_id"] == "REC-20260913-001"
    assert data["file_size_bytes"] == 104857600

    query_resp = await client.get("/api/v1/media/recordings?pit_id=pit-01")
    assert query_resp.status_code == 200
    recordings = query_resp.json()
    assert len(recordings) >= 1
    assert recordings[0]["segment_id"] == "REC-20260913-001"


@pytest.mark.asyncio
async def test_media_session_verify(client: AsyncClient) -> None:
    """Test camera live session authorization check."""
    resp = await client.get("/api/v1/media/session/verify?pit_id=pit-01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authorized"] is True
    assert "stream_url" in data
