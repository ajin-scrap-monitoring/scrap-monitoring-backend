"""Tests for health check endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthz(client: AsyncClient) -> None:
    """Test /healthz and /api/v1/healthz endpoints."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response_v1 = await client.get("/api/v1/healthz")
    assert response_v1.status_code == 200
    assert response_v1.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz(client: AsyncClient) -> None:
    """Test /api/v1/readyz database readiness check."""
    response = await client.get("/api/v1/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
