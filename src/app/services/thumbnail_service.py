"""Validate only JSON; Nginx and media deliver JPEG bytes after authorization."""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException

from src.app.core.config import get_settings
from src.app.schemas.daily_recording import DailyRecording
from src.app.schemas.media_service import ClipDetail
from src.app.schemas.thumbnail import ClipThumbnailInfo, DailyThumbnailInfo, ThumbnailRequest
from src.app.services.daily_recording_service import daily_recording_service
from src.app.services.media_client import MediaServiceError, media_client

Thumbnail = ClipThumbnailInfo | DailyThumbnailInfo


async def daily_thumbnail_for_date(recording_date: date) -> DailyThumbnailInfo | None:
    """Resolve one shared calendar resource without creating exports or image jobs."""
    try:
        next_date = recording_date + timedelta(days=1)
    except OverflowError as exc:
        raise HTTPException(422, "Recording date exceeds the supported calendar") from exc
    try:
        async with asyncio.timeout(get_settings().MEDIA_REQUEST_TIMEOUT_SECONDS):
            page = await daily_recording_service.list(
                {
                    "from_date": recording_date.isoformat(),
                    "to_date": next_date.isoformat(),
                    "limit": 2,
                }
            )
            # A camera has at most one recording per Korean date. Never choose
            # another day's image or silently accept ambiguous/paginated results.
            if page.next_cursor is not None or len(page.items) > 1:
                raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
            if not page.items:
                return None
            parent = page.items[0]
            if parent.recording_date != recording_date:
                raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")

            def validate_parent(current: DailyRecording) -> None:
                if current.recording_date != recording_date:
                    raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")

            _, info = await thumbnail(
                parent.recording_id, daily=True, validate_daily=validate_parent
            )
            assert isinstance(info, DailyThumbnailInfo)
            return info
    except TimeoutError as exc:
        raise MediaServiceError(504, "MEDIA_TIMEOUT", retryable=True) from exc


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
    validate_daily: Callable[[DailyRecording], None] | None = None,
) -> tuple[int, Thumbnail]:
    """A bounded selected-resource request never creates video exports or retries itself."""
    try:
        async with asyncio.timeout(get_settings().MEDIA_REQUEST_TIMEOUT_SECONDS):
            return await _thumbnail(
                identifier,
                daily=daily,
                prepare=prepare,
                validate_clip=validate_clip,
                validate_daily=validate_daily,
            )
    except TimeoutError as exc:
        raise MediaServiceError(504, "MEDIA_TIMEOUT", retryable=True) from exc


async def _thumbnail(
    identifier: UUID,
    *,
    daily: bool,
    prepare: ThumbnailRequest | None,
    validate_clip: Callable[[ClipDetail], None] | None,
    validate_daily: Callable[[DailyRecording], None] | None,
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
    if validate_daily is not None and isinstance(parent, DailyRecording):
        validate_daily(parent)
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
    # A daily JPEG may come from a completed TS while its MP4 archive is unfinished.
    # Clip thumbnails still require their independent clip resource to be ready.
    if info.status == "ready" and not daily and status != "ready":
        raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
    if response.status_code == 202 and info.status != "pending":
        raise MediaServiceError(502, "MEDIA_BAD_RESPONSE")
    return response.status_code, info.model_copy(
        update={"content_path": image if info.content_path else None}
    )
