"""Tests for load history and operational events endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from src.app.core.config import get_settings


@pytest.mark.asyncio
async def test_get_load_history_empty(client: AsyncClient) -> None:
    """Test load history endpoint when no metrics exist."""
    resp = await client.get("/api/v1/history/load?pit_id=pit-empty")
    assert resp.status_code == 200
    data = resp.json()
    assert "from" in data
    assert "to" in data
    assert "samples" in data
    assert "collectionThresholds" in data
    assert len(data["collectionThresholds"]) >= 1
    assert "eventMarkers" in data


@pytest.mark.asyncio
async def test_load_history_with_events_and_filters(client: AsyncClient) -> None:
    """Test load history with metrics, collection event, alert event, and category filter."""
    settings = get_settings()
    now = datetime.now(UTC)

    # 1. Ingest normal metric (yesterday)
    t1 = (now - timedelta(hours=2)).isoformat()
    resp1 = await client.post(
        "/api/v1/metrics/ingest",
        json={
            "pit_id": "pit-hist-test",
            "measured_at": t1,
            "calculated_height_cm": 90.0,
            "fill_ratio_percent": 30.0,
        },
        headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
    )
    assert resp1.status_code == 201

    # 2. Ingest critical metric (triggers alert log)
    t2 = (now - timedelta(hours=1)).isoformat()
    resp2 = await client.post(
        "/api/v1/metrics/ingest",
        json={
            "pit_id": "pit-hist-test",
            "measured_at": t2,
            "calculated_height_cm": 270.0,
            "fill_ratio_percent": 90.0,
        },
        headers={"X-Edge-API-Key": settings.EDGE_API_KEY},
    )
    assert resp2.status_code == 201

    # 3. Confirm collection (creates collection log)
    resp3 = await client.post(
        "/api/v1/collections/confirm",
        json={
            "pit_id": "pit-hist-test",
            "residual_height_cm": 15.0,
            "operator_name": "박작업자",
            "notes": "스크랩 수거",
        },
    )
    assert resp3.status_code == 201

    # 4. Query load history with all categories
    resp4 = await client.get("/api/v1/history/load?pit_id=pit-hist-test&category=all")
    assert resp4.status_code == 200
    hist_data = resp4.json()
    assert len(hist_data["samples"]) >= 2
    assert len(hist_data["eventMarkers"]) >= 1

    # 5. Query load history filtered by collection only
    resp5 = await client.get("/api/v1/history/load?pit_id=pit-hist-test&category=collection")
    assert resp5.status_code == 200
    col_hist = resp5.json()
    for marker in col_hist["eventMarkers"]:
        assert marker["category"] == "collection"


@pytest.mark.asyncio
async def test_list_operational_events_and_pagination(client: AsyncClient) -> None:
    """Test operational events list and pagination."""
    # 1. Ingest collection event
    confirm_resp = await client.post(
        "/api/v1/collections/confirm",
        json={
            "pit_id": "pit-event-test",
            "residual_height_cm": 20.0,
            "operator_name": "이반장",
            "notes": "스크랩 수거",
        },
    )
    assert confirm_resp.status_code == 201

    # 2. Query events
    resp = await client.get("/api/v1/events?pit_id=pit-event-test&page=1&pageSize=2")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "page" in data
    page_meta = data["page"]
    assert page_meta["page"] == 1
    assert page_meta["pageSize"] == 2
    assert page_meta["totalItems"] >= 1
    assert len(data["items"]) == 1

    first_event = data["items"][0]
    assert first_event["id"].startswith("evt-col-")
    assert first_event["type"] == "collection_completed"
    assert first_event["category"] == "collection"
    assert first_event["severity"] == "info"
    assert first_event["status"] == "completed"
    assert "이반장" in first_event["detail"]
