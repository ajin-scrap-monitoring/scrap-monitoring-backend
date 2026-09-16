"""Tests for WHEP WebRTC Streaming Signaling API."""

import pytest
from httpx import AsyncClient

SAMPLE_SDP_OFFER = (
    "v=0\r\n"
    "o=- 482710382 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=recvonly\r\n"
    "a=mid:0\r\n"
)

SAMPLE_ICE_FRAG = (
    "a=candidate:1 1 UDP 2122260223 192.168.0.10 50000 typ host\r\na=end-of-candidates\r\n"
)


@pytest.mark.asyncio
async def test_webrtc_create_session_success(client: AsyncClient) -> None:
    """Test successful WHEP WebRTC playback session creation."""
    resp = await client.post(
        "/api/v1/webrtc/streams/camera-main",
        content=SAMPLE_SDP_OFFER,
        headers={"Content-Type": "application/sdp"},
    )
    assert resp.status_code == 201
    assert "location" in resp.headers
    assert resp.headers["location"].startswith("/api/v1/webrtc/sessions/")
    assert "etag" in resp.headers
    assert resp.headers["content-type"].startswith("application/sdp")

    sdp_answer = resp.text
    assert "v=0" in sdp_answer
    assert "a=ice-options:trickle" in sdp_answer
    assert "a=sendonly" in sdp_answer


@pytest.mark.asyncio
async def test_webrtc_create_session_validation_errors(client: AsyncClient) -> None:
    """Test WHEP endpoint validation: Content-Type, empty body, invalid streamId."""
    # 1. Unsupported media type (JSON instead of application/sdp)
    resp_unsupported = await client.post(
        "/api/v1/webrtc/streams/camera-main",
        content='{"sdp": "v=0..."}',
        headers={"Content-Type": "application/json"},
    )
    assert resp_unsupported.status_code == 415

    # 2. Empty SDP offer
    resp_empty = await client.post(
        "/api/v1/webrtc/streams/camera-main",
        content="   ",
        headers={"Content-Type": "application/sdp"},
    )
    assert resp_empty.status_code == 422

    # 3. Invalid streamId pattern (e.g. invalid special chars)
    resp_invalid_id = await client.post(
        "/api/v1/webrtc/streams/@invalid-id!",
        content=SAMPLE_SDP_OFFER,
        headers={"Content-Type": "application/sdp"},
    )
    assert resp_invalid_id.status_code == 422


@pytest.mark.asyncio
async def test_webrtc_patch_ice_candidate(client: AsyncClient) -> None:
    """Test PATCH trickle ICE candidates and ETag validation."""
    # Create session first
    create_resp = await client.post(
        "/api/v1/webrtc/streams/camera-main",
        content=SAMPLE_SDP_OFFER,
        headers={"Content-Type": "application/sdp"},
    )
    assert create_resp.status_code == 201
    location = create_resp.headers["location"]
    etag = create_resp.headers["etag"]

    # 1. Valid PATCH with correct ETag
    patch_resp = await client.patch(
        location,
        content=SAMPLE_ICE_FRAG,
        headers={
            "Content-Type": "application/trickle-ice-sdpfrag",
            "If-Match": etag,
        },
    )
    assert patch_resp.status_code == 204

    # 2. PATCH with wrong ETag -> 412 Precondition Failed
    patch_wrong_etag = await client.patch(
        location,
        content=SAMPLE_ICE_FRAG,
        headers={
            "Content-Type": "application/trickle-ice-sdpfrag",
            "If-Match": '"wrong-etag-value"',
        },
    )
    assert patch_wrong_etag.status_code == 412

    # 3. PATCH non-existent session -> 404 Not Found
    patch_nonexistent = await client.patch(
        "/api/v1/webrtc/sessions/00000000-0000-0000-0000-000000000000",
        content=SAMPLE_ICE_FRAG,
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert patch_nonexistent.status_code == 404

    # 4. PATCH invalid UUID format -> 422
    patch_invalid_uuid = await client.patch(
        "/api/v1/webrtc/sessions/not-a-uuid",
        content=SAMPLE_ICE_FRAG,
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert patch_invalid_uuid.status_code == 422


@pytest.mark.asyncio
async def test_webrtc_delete_session_lifecycle(client: AsyncClient) -> None:
    """Test terminating a WebRTC playback session and verifying cleanup."""
    # Create session
    create_resp = await client.post(
        "/api/v1/webrtc/streams/camera-main",
        content=SAMPLE_SDP_OFFER,
        headers={"Content-Type": "application/sdp"},
    )
    assert create_resp.status_code == 201
    location = create_resp.headers["location"]

    # Delete session
    del_resp = await client.delete(location)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # Subsequent delete -> 404
    del_again = await client.delete(location)
    assert del_again.status_code == 404

    # Subsequent PATCH -> 404
    patch_after_del = await client.patch(
        location,
        content=SAMPLE_ICE_FRAG,
        headers={"Content-Type": "application/trickle-ice-sdpfrag"},
    )
    assert patch_after_del.status_code == 404
