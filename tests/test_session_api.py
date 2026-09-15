"""Tests for dashboard session and authentication endpoints."""

import pytest
from httpx import AsyncClient

from src.app.services.session_service import SESSION_COOKIE_NAME, session_service


@pytest.fixture(autouse=True)
def _clear_sessions() -> None:
    """Clear in-memory sessions before each test."""
    session_service.clear_all()


@pytest.mark.asyncio
async def test_session_unauthorized_without_cookie(client: AsyncClient) -> None:
    """GET /api/v1/session without cookie should return 401."""
    resp = await client.get("/api/v1/session")
    assert resp.status_code == 401
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_session_login_invalid_credentials(client: AsyncClient) -> None:
    """POST /api/v1/session with wrong password or unknown user should return 401."""
    # Unknown user
    resp_unknown = await client.post(
        "/api/v1/session",
        json={"username": "unknown_user", "password": "password", "persistent": False},
    )
    assert resp_unknown.status_code == 401

    # Wrong password
    resp_wrong_pw = await client.post(
        "/api/v1/session",
        json={"username": "admin", "password": "wrongpassword", "persistent": False},
    )
    assert resp_wrong_pw.status_code == 401


@pytest.mark.asyncio
async def test_session_login_admin_success_and_get_session(client: AsyncClient) -> None:
    """POST /api/v1/session for admin should set cookie and allow GET /api/v1/session."""
    login_payload = {
        "username": "admin",
        "password": "admin1234",
        "persistent": False,
    }
    login_resp = await client.post("/api/v1/session", json=login_payload)
    assert login_resp.status_code == 200
    data = login_resp.json()
    assert data["user"]["id"] == "admin"
    assert data["user"]["displayName"] == "관리자"
    assert data["user"]["role"] == "administrator"
    assert "csrfToken" in data
    assert "expiresAt" in data

    # Verify cookie was set
    assert SESSION_COOKIE_NAME in login_resp.cookies
    session_cookie_value = login_resp.cookies[SESSION_COOKIE_NAME]
    assert session_cookie_value is not None

    # Query current session with cookie
    get_resp = await client.get(
        "/api/v1/session",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={session_cookie_value}"},
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["user"]["id"] == "admin"
    assert get_data["csrfToken"] == data["csrfToken"]


@pytest.mark.asyncio
async def test_session_login_operator_persistent(client: AsyncClient) -> None:
    """POST /api/v1/session for operator with persistent=True should have viewer role."""
    login_payload = {
        "username": "operator",
        "password": "operator1234",
        "persistent": True,
    }
    resp = await client.post("/api/v1/session", json=login_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["id"] == "operator"
    assert data["user"]["displayName"] == "현장 운영자"
    assert data["user"]["role"] == "viewer"
    assert SESSION_COOKIE_NAME in resp.cookies


@pytest.mark.asyncio
async def test_session_delete_and_csrf_validation(client: AsyncClient) -> None:
    """DELETE /api/v1/session should validate CSRF and invalidate session."""
    # 1. Login
    login_resp = await client.post(
        "/api/v1/session",
        json={"username": "admin", "password": "admin1234", "persistent": False},
    )
    assert login_resp.status_code == 200
    session_cookie = login_resp.cookies[SESSION_COOKIE_NAME]
    csrf_token = login_resp.json()["csrfToken"]

    cookie_header = {"Cookie": f"{SESSION_COOKIE_NAME}={session_cookie}"}

    # 2. DELETE with invalid CSRF token should return 403 Forbidden
    fail_delete = await client.delete(
        "/api/v1/session",
        headers={**cookie_header, "X-CSRF-Token": "invalid-csrf-token"},
    )
    assert fail_delete.status_code == 403

    # 3. DELETE with valid CSRF token should succeed (204 No Content)
    success_delete = await client.delete(
        "/api/v1/session",
        headers={**cookie_header, "X-CSRF-Token": csrf_token},
    )
    assert success_delete.status_code == 204

    # 4. GET /api/v1/session should now return 401
    after_resp = await client.get(
        "/api/v1/session",
        headers=cookie_header,
    )
    assert after_resp.status_code == 401

    # 5. DELETE without cookie should return 401
    unauth_delete = await client.delete("/api/v1/session")
    assert unauth_delete.status_code == 401
