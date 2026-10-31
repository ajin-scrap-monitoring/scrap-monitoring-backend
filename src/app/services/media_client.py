"""Authenticated JSON-only client for the camera media service."""

import asyncio
from typing import Any, TypeVar
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from src.app.core.config import get_settings
from src.app.schemas.media_info import ClipMediaInfo
from src.app.schemas.media_service import (
    ClipDetail,
    ClipPage,
    ClipReceipt,
    ClipRequest,
    RangeClipRequest,
    RecordingPolicy,
)

Schema = TypeVar("Schema", bound=BaseModel)
# A bounded file probe may take fifteen seconds; leave three seconds for JSON I/O.
MEDIA_INFO_TIMEOUT_SECONDS = 18.0


class MediaServiceError(Exception):
    """Safe public error: never include upstream messages, URLs or service tokens."""

    def __init__(self, status_code: int, code: str, retryable: bool = False):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class MediaClient:
    """Reuse asynchronous connections and never fetch live streams or MP4 bodies."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        expected: tuple[int, ...],
        payload: BaseModel | None = None,
        params: dict[str, str | int] | None = None,
        *,
        daily: bool = False,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        settings = get_settings()
        base_url = settings.MEDIA_SERVICE_URL
        headers = {}
        if daily:
            base_url = settings.MEDIA_DAILY_SERVICE_URL or (
                base_url.rstrip("/")[:-2] + "v2" if base_url else None
            )
        elif settings.MEDIA_API_TOKEN:
            headers["Authorization"] = f"Bearer {settings.MEDIA_API_TOKEN.get_secret_value()}"
        if not base_url or (not daily and not settings.MEDIA_API_TOKEN):
            raise MediaServiceError(503, "MEDIA_NOT_CONFIGURED")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=settings.MEDIA_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
        try:
            response = await self._client.request(
                method,
                f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=headers,
                json=payload.model_dump(mode="json") if payload else None,
                params=params,
                timeout=httpx.USE_CLIENT_DEFAULT if timeout_seconds is None else timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise MediaServiceError(504, "MEDIA_TIMEOUT", retryable=True) from exc
        except httpx.RequestError as exc:
            raise MediaServiceError(503, "MEDIA_UNAVAILABLE", retryable=True) from exc
        if response.status_code not in expected:
            # Only contract codes are forwarded; arbitrary error text may contain secrets.
            known_codes = {
                "DAILY_RECORDING_NOT_FOUND",
                "DAY_NOT_CLOSED",
                "DAY_FINALIZING",
                "NO_RECORDING_DATA",
                "PLAYBACK_NOT_READY",
                "EXPORT_NOT_READY",
                "RECORDING_ACTIVE",
                "RECORDING_NOT_ENABLED",
                "RECORDING_NOT_READY",
                "RECORDING_NOT_FOUND_AT_TIME",
                "EVENT_TIME_IN_FUTURE",
                "REQUEST_TIME_IN_FUTURE",
                "RECORDING_NOT_FOUND_IN_RANGE",
                "IDEMPOTENCY_CONFLICT",
                "RETRY_NOT_ALLOWED",
                "CLIP_PREPARATION_FAILED",
                "EXPORT_IN_PROGRESS",
                "DAILY_RECORDING_DELETED",
                "DAILY_RECORDING_EXPIRED",
                "INSUFFICIENT_STORAGE",
                "EXPORT_CAPACITY_EXCEEDED",
                "RECORDING_STORAGE_ERROR",
                "INVALID_ARGUMENT",
                "INVALID_CURSOR",
                "CAMERA_NOT_FOUND",
                "CLIP_NOT_FOUND",
                "CLIP_NOT_READY",
                "CLIP_IN_PROGRESS",
                "CLIP_DELETED",
                "CLIP_EXPIRED",
                "UNSUPPORTED_MEDIA_TYPE",
                "CAMERA_UNAVAILABLE",
                "STORAGE_UNAVAILABLE",
                "SERVICE_BUSY",
                "METADATA_CAPACITY_EXCEEDED",
                "INTERNAL_ERROR",
            }
            code = "MEDIA_BAD_RESPONSE"
            try:
                raw: Any = response.json()
                remote_code = raw.get("error", {}).get("code") if isinstance(raw, dict) else None
                if isinstance(remote_code, str) and remote_code in known_codes:
                    code = remote_code
            except (ValueError, AttributeError):
                pass
            if response.status_code in (401, 403):
                raise MediaServiceError(502, "MEDIA_AUTH_FAILED")
            retryable = (
                response.status_code in (408, 429)
                or response.status_code >= 500
                or response.status_code == 409
                and code == "RECORDING_NOT_READY"
            )
            public_status = (
                response.status_code
                if response.status_code
                in (
                    400,
                    404,
                    409,
                    410,
                    415,
                    416,
                    429,
                    503,
                )
                else 502
            )
            raise MediaServiceError(public_status, code, retryable=retryable)
        return response

    def _parse(self, response: httpx.Response, schema: type[Schema]) -> Schema:
        try:
            return schema.model_validate_json(response.content)
        except ValidationError as exc:
            raise MediaServiceError(502, "MEDIA_BAD_RESPONSE") from exc

    async def get_policy(self, camera_id: str) -> RecordingPolicy:
        response = await self._request(
            "GET", f"cameras/{quote(camera_id, safe='')}/recording-policy", (200,)
        )
        return self._parse(response, RecordingPolicy)

    async def put_policy(self, camera_id: str, policy: RecordingPolicy) -> RecordingPolicy:
        response = await self._request(
            "PUT", f"cameras/{quote(camera_id, safe='')}/recording-policy", (200,), policy
        )
        return self._parse(response, RecordingPolicy)

    async def create_clip(self, request: ClipRequest) -> tuple[int, ClipReceipt]:
        response = await self._request("POST", "clips", (200, 202), request)
        receipt = self._parse(response, ClipReceipt)
        if receipt.event_id != request.event_id or receipt.camera_id != request.camera_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return response.status_code, receipt

    async def get_clip(self, clip_id: UUID) -> ClipDetail:
        response = await self._request("GET", f"clips/{clip_id}", (200,))
        detail = self._parse(response, ClipDetail)
        if detail.clip_id != clip_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return detail

    async def metadata(self, path: str, schema: type[Schema], *, daily: bool = False) -> Schema:
        """Read JSON measurements within a wall-clock budget, without automatic retries."""
        try:
            async with asyncio.timeout(MEDIA_INFO_TIMEOUT_SECONDS):
                response = await self._request(
                    "GET",
                    path,
                    (200,),
                    daily=daily,
                    timeout_seconds=MEDIA_INFO_TIMEOUT_SECONDS,
                )
                return self._parse(response, schema)
        except TimeoutError as exc:
            raise MediaServiceError(504, "MEDIA_TIMEOUT", retryable=True) from exc

    async def get_clip_media_info(self, clip_id: UUID) -> ClipMediaInfo:
        detail = await self.get_clip(clip_id)
        if detail.camera_id != get_settings().MEDIA_CAMERA_ID:
            raise MediaServiceError(404, "CAMERA_NOT_FOUND")
        if detail.status in ("deleted", "expired"):
            raise MediaServiceError(410, "CLIP_" + detail.status.upper())
        if detail.status != "ready":
            raise MediaServiceError(409, "CLIP_NOT_READY")
        info = await self.metadata(f"clips/{clip_id}/media-info", ClipMediaInfo)
        if info.clip_id != clip_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return info

    async def create_range_clip(self, request: RangeClipRequest) -> tuple[int, ClipReceipt]:
        response = await self._request("POST", "clips", (200, 202), request)
        receipt = self._parse(response, ClipReceipt)
        if receipt.event_id != request.event_id or receipt.camera_id != request.camera_id:
            raise MediaServiceError(502, "MEDIA_IDENTITY_MISMATCH")
        return response.status_code, receipt

    async def list_clips(self, params: dict[str, str | int]) -> ClipPage:
        return self._parse(await self._request("GET", "clips", (200,), params=params), ClipPage)

    async def delete_clip(self, clip_id: UUID) -> None:
        await self._request("DELETE", f"clips/{clip_id}", (204,))


media_client = MediaClient()
