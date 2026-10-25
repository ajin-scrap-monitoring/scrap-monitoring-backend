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


@pytest.fixture
async def overlapping_legacy_recording(client):
    payload = {
        "segment_id": "synthetic-overlap",
        "pit_id": "synthetic-pit",
        "start_time": "2030-01-01T09:00:00Z",
        "end_time": "2030-01-01T10:00:00Z",
        "file_path": "/synthetic/clip.mp4",
        "file_size_bytes": 1024,
        "duration_seconds": 3600,
    }
    assert (await client.post("/api/v1/media/recordings/complete", json=payload)).status_code == 201
    return payload


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({"from": "2030-01-01T09:30:00Z", "to": "2030-01-01T09:45:00Z"}, 1),
        ({"from": "2030-01-01T08:30:00Z", "to": "2030-01-01T09:30:00Z"}, 1),
        ({"from": "2030-01-01T09:30:00Z", "to": "2030-01-01T10:30:00Z"}, 1),
        ({"from": "2030-01-01T09:00:00Z", "to": "2030-01-01T10:00:00Z"}, 1),
        ({"from": "2030-01-01T08:00:00Z", "to": "2030-01-01T09:00:00Z"}, 0),
        # Keep the existing event-clip contract: end >= from and start < to.
        ({"from": "2030-01-01T10:00:00Z", "to": "2030-01-01T11:00:00Z"}, 1),
        ({"from": "2030-01-01T10:00:00.000001Z"}, 0),
        ({"from": "2030-01-01T09:30:00Z"}, 1),
        ({"to": "2030-01-01T09:30:00Z"}, 1),
        ({"from": "2030-01-01T18:30:00+09:00", "to": "2030-01-01T18:45:00+09:00"}, 1),
    ],
)
async def test_overlapping_legacy_recording_time_filters(
    client, overlapping_legacy_recording, bounds, expected
):
    response = await client.get("/api/v1/recordings", params={"pit_id": "synthetic-pit", **bounds})
    assert response.status_code == 200
    data = response.json()
    assert data["page"]["totalItems"] == expected
    assert [item["id"] for item in data["items"]] == (["synthetic-overlap"] if expected else [])


async def test_overlapping_legacy_recording_pagination_and_pit_category_filters(
    client, overlapping_legacy_recording
):
    original = overlapping_legacy_recording
    for identifier, pit, start, end in [
        ("synthetic-later", "synthetic-pit", "09:20:00", "09:50:00"),
        ("synthetic-before", "synthetic-pit", "08:00:00", "08:30:00"),
        ("synthetic-after", "synthetic-pit", "10:30:00", "11:00:00"),
        ("synthetic-other-pit", "other-pit", "09:20:00", "09:50:00"),
    ]:
        payload = {
            **original,
            "segment_id": identifier,
            "pit_id": pit,
            "start_time": f"2030-01-01T{start}Z",
            "end_time": f"2030-01-01T{end}Z",
            "duration_seconds": 1800,
        }
        assert (
            await client.post("/api/v1/media/recordings/complete", json=payload)
        ).status_code == 201
    params = {
        "pit_id": "synthetic-pit",
        "from": "2030-01-01T09:30:00Z",
        "to": "2030-01-01T09:45:00Z",
        "category": "collection",
        "pageSize": 1,
    }
    ids = []
    for page in (1, 2):
        response = await client.get("/api/v1/recordings", params={**params, "page": page})
        assert response.status_code == 200
        data = response.json()
        assert data["page"]["totalItems"] == data["page"]["totalPages"] == 2
        ids.extend(item["id"] for item in data["items"])
    assert ids == ["synthetic-later", "synthetic-overlap"]
    for category in ("alert", "error"):
        data = (
            await client.get("/api/v1/recordings", params={**params, "category": category})
        ).json()
        assert data["items"] == [] and data["page"]["totalItems"] == 0
