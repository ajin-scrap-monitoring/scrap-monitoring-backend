"""Tests for Edge Platform measurement contract v1.0 and heartbeat integration."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_edge_authentication_bearer_and_legacy(client: AsyncClient) -> None:
    """Verify both Authorization: Bearer and X-Edge-API-Key authentication."""
    valid_payload = {
        "schema_version": "1.0",
        "measurement_id": "AUTH-TEST-001",
        "site_id": "pit-01",
        "edge_id": "edge-01",
        "measured_at": datetime.now(UTC).isoformat(),
        "overall_quality": "GOOD",
        "fill_percentage": 50.0,
    }

    # 1. Missing header -> 401
    resp_no_auth = await client.post("/api/v1/metrics/ingest", json=valid_payload)
    assert resp_no_auth.status_code == 401

    # 2. Invalid Bearer token -> 401
    resp_bad_bearer = await client.post(
        "/api/v1/metrics/ingest",
        json=valid_payload,
        headers={"Authorization": "Bearer wrong-token-1234"},
    )
    assert resp_bad_bearer.status_code == 401

    # 3. Valid Bearer token -> 200 OK
    resp_valid_bearer = await client.post(
        "/api/v1/metrics/ingest",
        json=valid_payload,
        headers={"Authorization": "Bearer dev-edge-secret-key-12345"},
    )
    assert resp_valid_bearer.status_code == 200
    data = resp_valid_bearer.json()
    assert data["measurement_id"] == "AUTH-TEST-001"
    assert data["accepted"] is True


@pytest.mark.asyncio
async def test_edge_metric_ingest_idempotency_and_ack(client: AsyncClient) -> None:
    """Verify measurement v1.0 contract ingestion, idempotency ACK, and conflict handling."""
    auth_header = {"Authorization": "Bearer dev-edge-secret-key-12345"}

    payload = {
        "schema_version": "1.0",
        "measurement_id": "MEAS-20260916-100",
        "cycle_id": "CYC-01",
        "site_id": "pit-01",
        "edge_id": "edge-pi-01",
        "measured_at": "2026-09-16T15:00:00Z",
        "calibration_version": "calib-v2",
        "config_revision": "rev-3",
        "overall_quality": "GOOD",
        "fill_ratio": 0.85,
        "fill_percentage": 85.0,
        "sensors": [
            {
                "sequence": 1,
                "instance": "lidar-1",
                "height": 2550.0,
                "valid_sample_ratio": 0.98,
                "coverage": 0.95,
                "status": "OK",
            },
            {
                "sequence": 2,
                "instance": "lidar-2",
                "height": 2550.0,
                "valid_sample_ratio": 0.97,
                "coverage": 0.94,
                "status": "OK",
            },
        ],
    }

    # 1. Initial Ingest -> 200 OK with accepted=True
    resp1 = await client.post(
        "/api/v1/metrics/ingest",
        json=payload,
        headers=auth_header,
    )
    assert resp1.status_code == 200
    ack1 = resp1.json()
    assert ack1["measurement_id"] == "MEAS-20260916-100"
    assert ack1["accepted"] is True

    # 2. Resend exact same measurement (network retry) -> 200 OK with duplicate=True
    resp2 = await client.post(
        "/api/v1/metrics/ingest",
        json=payload,
        headers=auth_header,
    )
    assert resp2.status_code == 200
    ack2 = resp2.json()
    assert ack2["measurement_id"] == "MEAS-20260916-100"
    assert ack2["accepted"] is True
    assert ack2.get("duplicate") is True

    # 3. Resend same measurement_id with conflicting payload -> 409 Conflict
    conflict_payload = dict(payload)
    conflict_payload["fill_percentage"] = 95.0
    conflict_payload["sensors"] = []
    resp_conflict = await client.post(
        "/api/v1/metrics/ingest",
        json=conflict_payload,
        headers=auth_header,
    )
    assert resp_conflict.status_code == 409


@pytest.mark.asyncio
async def test_edge_metric_ingest_invalid_quality_handled(client: AsyncClient) -> None:
    """Verify INVALID quality measurement without fill_percentage is accepted and stored."""
    auth_header = {"Authorization": "Bearer dev-edge-secret-key-12345"}

    invalid_payload = {
        "schema_version": "1.0",
        "measurement_id": "MEAS-INVALID-001",
        "site_id": "pit-01",
        "edge_id": "edge-pi-01",
        "measured_at": "2026-09-16T15:05:00Z",
        "overall_quality": "INVALID",
        "quality_reason_code": "SENSOR_OCCLUDED",
        "fill_ratio": None,
        "fill_percentage": None,
        "sensors": [
            {
                "sequence": 1,
                "instance": "lidar-1",
                "height": None,
                "status": "ERROR",
            }
        ],
    }

    resp = await client.post(
        "/api/v1/metrics/ingest",
        json=invalid_payload,
        headers=auth_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["measurement_id"] == "MEAS-INVALID-001"
    assert data["accepted"] is True


@pytest.mark.asyncio
async def test_edge_heartbeat_lifecycle(client: AsyncClient) -> None:
    """Verify edge heartbeat report endpoint and auth."""
    auth_header = {"Authorization": "Bearer dev-edge-secret-key-12345"}

    heartbeat_payload = {
        "schema_version": "1.0",
        "site_id": "pit-01",
        "edge_id": "edge-pi-01",
        "config_revision": "rev-10",
        "deployment_revision": "deploy-v1.2",
        "camera_id": "cam-01",
        "reported_at": "2026-09-16T15:10:00Z",
        "overall_status": "HEALTHY",
        "status_reason_code": None,
        "clock_sync_status": "LOCKED",
        "clock_offset_ms": 2.5,
        "services": {
            "lidar_daemon": "RUNNING",
            "camera_streamer": "RUNNING",
            "uplink_worker": "RUNNING",
        },
    }

    # 1. Without auth -> 401
    unauth_resp = await client.post("/api/v1/edge/heartbeat", json=heartbeat_payload)
    assert unauth_resp.status_code == 401

    # 2. With valid auth -> 200 OK
    resp = await client.post(
        "/api/v1/edge/heartbeat",
        json=heartbeat_payload,
        headers=auth_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["edge_id"] == "edge-pi-01"

    # 3. Subsequent heartbeat updates existing record
    heartbeat_payload["overall_status"] = "DEGRADED"
    heartbeat_payload["status_reason_code"] = "HIGH_TEMP"
    resp2 = await client.post(
        "/api/v1/edge/heartbeat",
        json=heartbeat_payload,
        headers=auth_header,
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "ok"
