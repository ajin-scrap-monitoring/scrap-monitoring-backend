"""Validate only JSON; Nginx and media deliver JPEG bytes after authorization."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from src.app.core.config import get_settings
from src.app.schemas.daily_recording import DailyRecording
from src.app.schemas.media_service import ClipDetail
from src.app.schemas.thumbnail import ClipThumbnailInfo, DailyThumbnailInfo, ThumbnailRequest
from src.app.services.media_client import MediaServiceError, media_client

Thumbnail = ClipThumbnailInfo | DailyThumbnailInfo


def paths(identifier: UUID, daily: bool) -> tuple[str, str, str]:
    resource = "daily-recordings" if daily else "clips"
    prefix = f"{resource}/{identifier}"
    public = f"/api/v2/recordings/daily/{identifier}" if daily else f"/api/v1/media/{prefix}"
    return prefix, public, f"/media/{prefix}/thumbnail"


async def thumbnail(
    identifier: UUID,
    *,
    daily: bool = False,
    prepare: ThumbnailRequest | None = None,
    validate_clip: Callable[[ClipDetail], None] | None = None,
) -> tuple[int, Thumbnail]:
    """A bounded selected-resource request never creates video exports or retries itself."""
    try:
        async with asyncio.timeout(get_settings().MEDIA_REQUEST_TIMEOUT_SECONDS):
            return await _thumbnail(
                identifier, daily=daily, prepare=prepare, validate_clip=validate_clip
            )
    except TimeoutError as exc:
        raise MediaServiceError(504, "MEDIA_TIMEOUT", retryable=True) from exc


async def _thumbnail(
    identifier: UUID,
    *,
    daily: bool,
    prepare: ThumbnailRequest | None,
    validate_clip: Callable[[ClipDetail], None] | None,
) -> tuple[int, Thumbnail]:
    prefix, _, image = paths(identifier, daily)
    response = await media_client._request("GET", prefix, (200,), daily=daily, tokenless=True)
    parent: DailyRecording | ClipDetail
    if daily:
        parent = media_client._parse(response, DailyRecording)
        parent_id = parent.recording_id
        expiries = [parent.expires_at, parent.export_expires_at]
    else:
        parent = media_client._parse(response, ClipDetail)
        parent_id = parent.clip_id
        expiries = [parent.expires_at]
    if parent_id != identifier:
        raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
    if parent.camera_id != get_settings().MEDIA_CAMERA_ID:
        raise MediaServiceError(404, "CAMERA_NOT_FOUND")
    status = parent.status
    if status in ("deleted", "expired") or any(
        expiry is not None and expiry <= datetime.now(UTC) for expiry in expiries
    ):
        state = "DELETED" if status == "deleted" else "EXPIRED"
        raise MediaServiceError(410, ("DAILY_RECORDING_" if daily else "CLIP_") + state)
    if validate_clip is not None and isinstance(parent, ClipDetail):
        validate_clip(parent)
    suffix = "thumbnail" if prepare is not None else "thumbnail-info"
    try:
        response = await media_client._request(
            "POST" if prepare is not None else "GET",
            f"{prefix}/{suffix}",
            (200, 202) if prepare is not None else (200,),
            prepare,
            daily=daily,
            tokenless=True,
        )
    except MediaServiceError as exc:
        # A verified parent on an older server may not have the dedicated info route.
        if prepare is not None or exc.status_code != 404 or exc.code != "MEDIA_BAD_RESPONSE":
            raise
        key = "recording_id" if daily else "clip_id"
        unsupported = {
            key: identifier,
            "camera_id": parent.camera_id,
            "status": "unsupported",
            "representative_at": None,
            "time_basis": None,
            "content_path": None,
            "width": None,
            "height": None,
            "size_bytes": None,
            "generated_at": None,
            "reason_code": None,
        }
        return 200, (
            DailyThumbnailInfo.model_validate(unsupported)
            if daily
            else ClipThumbnailInfo.model_validate(unsupported)
        )
    info: Thumbnail = (
        media_client._parse(response, DailyThumbnailInfo)
        if daily
        else media_client._parse(response, ClipThumbnailInfo)
    )
    info_id = info.recording_id if isinstance(info, DailyThumbnailInfo) else info.clip_id
    if info_id != identifier or info.camera_id != parent.camera_id:
        raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
    remote = f"/api/{'v2' if daily else 'v1'}/{prefix}/thumbnail"
    if info.content_path not in (None, remote):
        raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
    if info.status == "ready" and (
        (
            isinstance(parent, DailyRecording)
            and (status != "closed" or parent.export_state != "ready")
        )
        or (not daily and status != "ready")
    ):
        raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
    if response.status_code == 202 and info.status != "pending":
        raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
    return response.status_code, info.model_copy(
        update={"content_path": image if info.content_path else None}
    )
