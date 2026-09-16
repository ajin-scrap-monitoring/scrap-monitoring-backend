"""WHEP WebRTC Streaming Endpoints conforming to proposal/openapi.yaml."""

import logging
import uuid

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response, status

from src.app.services.webrtc_service import STREAM_ID_REGEX, webrtc_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webrtc", tags=["Streaming"])


@router.post(
    "/streams/{streamId}",
    status_code=status.HTTP_201_CREATED,
    summary="Create a WHEP-shaped WebRTC playback session",
    responses={
        201: {
            "description": "WebRTC session created with required ETag and a Trickle ICE SDP answer",
            "content": {"application/sdp": {}},
            "headers": {
                "Location": {"description": "Session resource URL", "schema": {"type": "string"}},
                "ETag": {
                    "description": "Entity tag of negotiated session",
                    "schema": {"type": "string"},
                },
            },
        },
        400: {"description": "Bad Request"},
        404: {"description": "Stream not found"},
        415: {"description": "Unsupported Media Type"},
        422: {"description": "Validation Error"},
    },
)
async def create_webrtc_session(
    request: Request,
    response: Response,
    streamId: str = Path(
        ...,
        description="Stream identifier (e.g. camera-main)",
        min_length=1,
        max_length=128,
    ),
) -> Response:
    """Create a WHEP playback session using browser SDP offer."""
    # 1. Validate streamId regex pattern
    if not STREAM_ID_REGEX.match(streamId):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid streamId pattern: '{streamId}'",
        )

    # 2. Validate Content-Type: must be application/sdp
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/sdp"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Expected 'application/sdp' Content-Type, got '{content_type}'",
        )

    # 3. Read raw SDP Offer body
    body_bytes = await request.body()
    sdp_offer = body_bytes.decode("utf-8", errors="replace").strip()
    if not sdp_offer:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="SDP offer body cannot be empty",
        )

    # 4. Create session via WebRTCSessionService
    try:
        session_id, etag, sdp_answer = await webrtc_service.create_session(
            stream_id=streamId,
            sdp_offer=sdp_offer,
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(val_err),
        ) from val_err
    except RuntimeError as run_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(run_err),
        ) from run_err

    # 5. Build WHEP standard response
    location_url = f"/api/v1/webrtc/sessions/{session_id}"
    return Response(
        content=sdp_answer,
        status_code=status.HTTP_201_CREATED,
        media_type="application/sdp",
        headers={
            "Location": location_url,
            "ETag": etag,
        },
    )


@router.patch(
    "/sessions/{sessionId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Send an SDP answer or trickle ICE fragment to a WebRTC session",
    responses={
        204: {"description": "SDP answer or ICE fragment accepted"},
        404: {"description": "Session not found or expired"},
        412: {"description": "Precondition Failed (ETag mismatch)"},
        415: {"description": "Unsupported Media Type"},
    },
)
async def update_webrtc_session(
    request: Request,
    sessionId: str = Path(..., description="UUID of the active WebRTC playback session"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    """Process Trickle ICE candidate or SDP answer PATCH."""
    # 1. Validate UUID format
    try:
        uuid.UUID(sessionId)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sessionId UUID format: '{sessionId}'",
        ) from err

    # 2. Validate Content-Type
    content_type = request.headers.get("content-type", "")
    allowed_types = ("application/trickle-ice-sdpfrag", "application/sdp")
    if not any(content_type.startswith(t) for t in allowed_types):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Expected Content-Type in {allowed_types}, got '{content_type}'",
        )

    # 3. Read body
    body_bytes = await request.body()
    ice_data = body_bytes.decode("utf-8", errors="replace").strip()

    # 4. Update session
    try:
        await webrtc_service.update_ice_candidate(
            session_id=sessionId,
            ice_data=ice_data,
            if_match=if_match,
        )
    except KeyError as key_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"WebRTC session '{sessionId}' not found or expired",
        ) from key_err
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=str(val_err),
        ) from val_err

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/sessions/{sessionId}",
    status_code=status.HTTP_200_OK,
    summary="Delete a WebRTC playback session",
    responses={
        200: {"description": "WebRTC session deleted"},
        404: {"description": "Session not found"},
    },
)
async def delete_webrtc_session(
    sessionId: str = Path(..., description="UUID of the WebRTC playback session to terminate"),
) -> dict[str, str]:
    """Terminate and release a WebRTC playback session."""
    # Validate UUID format
    try:
        uuid.UUID(sessionId)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sessionId UUID format: '{sessionId}'",
        ) from err

    try:
        await webrtc_service.delete_session(session_id=sessionId)
    except KeyError as key_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"WebRTC session '{sessionId}' not found or already terminated",
        ) from key_err

    return {"status": "deleted", "sessionId": sessionId}
