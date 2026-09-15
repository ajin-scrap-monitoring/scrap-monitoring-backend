"""Tests for recordings dashboard endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_recordings_list_empty(client: AsyncClient) -> None:
    """Test empty recordings list."""
    resp = await client.get("/api/v1/recordings?pit_id=pit-empty")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "page" in data
    assert data["page"]["totalItems"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_recordings_list_and_get_by_id(client: AsyncClient) -> None:
    """Test recording ingestion, listing with pagination, and getting by ID."""
    # 1. Ingest recording metadata via media webhook
    webhook_payload = {
        "segment_id": "REC-TEST-20260915-01",
        "pit_id": "pit-rec-test",
        "start_time": "2026-09-15T09:00:00Z",
        "end_time": "2026-09-15T10:00:00Z",
        "file_path": "/srv/scrap-monitoring/media/pit-rec-test/rec-01.mp4",
        "file_size_bytes": 104857600,
        "duration_seconds": 3600.0,
    }
    hook_resp = await client.post("/api/v1/media/recordings/complete", json=webhook_payload)
    assert hook_resp.status_code == 201

    # 2. Query recordings list for dashboard
    list_resp = await client.get("/api/v1/recordings?pit_id=pit-rec-test&page=1&pageSize=10")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["page"]["totalItems"] == 1
    assert len(list_data["items"]) == 1

    item = list_data["items"][0]
    assert item["id"] == "REC-TEST-20260915-01"
    assert item["containerFormat"] == "mp4"
    assert item["videoCodec"] == "h264"
    assert item["durationSeconds"] == 3600
    assert item["thumbnailPath"] == "/media/thumbnails/REC-TEST-20260915-01.jpg"
    assert item["contentPath"] == "/media/recordings/REC-TEST-20260915-01.mp4"
    assert item["downloadPath"] == "/media/downloads/REC-TEST-20260915-01.mp4"

    # 3. Query single recording by ID
    get_resp = await client.get("/api/v1/recordings/REC-TEST-20260915-01")
    assert get_resp.status_code == 200
    rec = get_resp.json()
    assert rec["id"] == "REC-TEST-20260915-01"
    assert rec["sizeBytes"] == 104857600
    assert rec["status"] == "available"

    # 4. Query non-existent recording ID returns 404
    not_found_resp = await client.get("/api/v1/recordings/NON-EXISTENT")
    assert not_found_resp.status_code == 404
