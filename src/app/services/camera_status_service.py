"""Read-only, demand-driven camera health; bounded lifetime, body size and request budget."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from src.app.core.config import get_settings
from src.app.schemas.active_alerts_v2 import ActiveHealthAlertV2, ActiveHealthCoverageV2
from src.app.schemas.camera_status import CameraObservation, CameraStatusV2

MAX_BODY = 16 * 1024
REFRESH_SECONDS = 2.0
FETCH_SECONDS = 1.0
TOTAL_SECONDS = 1.25
Binding = tuple[str, str, str]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class CachedStatus:
    binding: Binding
    value: CameraStatusV2
    refresh_at: float
    deadline: float
    received_at: datetime


class ProbeFailure(Exception):
    pass


class CameraStatusService:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.clock, self.wall_clock = clock, wall_clock
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._cached: CachedStatus | None = None
        self._last: tuple[Binding, datetime, float] | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._cached = self._last = None

    @staticmethod
    def binding(pit: str) -> Binding | None:
        settings = get_settings()
        if pit != settings.MEDIA_PIT_ID:
            return None
        base = settings.MEDIA_SERVICE_URL
        if not base and settings.MEDIA_DAILY_SERVICE_URL:
            base = settings.MEDIA_DAILY_SERVICE_URL.rstrip("/")[:-2] + "v1"
        return (pit, base.rstrip("/"), settings.MEDIA_CAMERA_ID) if base else None

    def unknown(
        self, pit: str, camera: str | None, reason: str, *, unsupported: bool = False
    ) -> CameraStatusV2:
        return CameraStatusV2(
            pitId=pit,
            cameraId=camera,
            state="UNSUPPORTED" if unsupported else "UNKNOWN",
            computedAt=self.wall_clock(),
            reasonCodes=[reason],
        )

    def cached(self, binding: Binding) -> CameraStatusV2 | None:
        saved = self._cached
        wall, steady = self.wall_clock(), self.clock()
        if (
            saved is None
            or saved.binding != binding
            or steady >= saved.refresh_at
            or wall < saved.received_at
        ):
            return None
        if saved.value.validUntilAt is not None and (
            wall >= saved.value.validUntilAt or steady >= saved.deadline
        ):
            return None
        return saved.value.model_copy(
            deep=True,
            update={
                "computedAt": wall,
                "validForSeconds": max(
                    0.0,
                    min(saved.deadline - steady, (saved.value.validUntilAt - wall).total_seconds()),
                )
                if saved.value.validUntilAt
                else None,
            },
        )

    async def get(self, pit: str) -> CameraStatusV2:
        binding = self.binding(pit)
        if binding is None:
            return self.unknown(pit, None, "CAMERA_HEALTH_SOURCE_NOT_CONFIGURED", unsupported=True)
        saved = self.cached(binding)
        if saved is not None:
            return saved
        try:
            async with asyncio.timeout(TOTAL_SECONDS):
                async with self._lock:
                    saved = self.cached(binding)
                    if saved is not None:
                        return saved
                    started = self.clock()
                    try:
                        async with asyncio.timeout(FETCH_SECONDS):
                            observation = await self.observe(binding)
                        value, deadline = self.interpret(binding, observation, started)
                    except (TimeoutError, httpx.TimeoutException):
                        value, deadline = (
                            self.unknown(pit, binding[2], "CAMERA_STATUS_TIMEOUT"),
                            self.clock(),
                        )
                    except httpx.RequestError:
                        value, deadline = (
                            self.unknown(pit, binding[2], "CAMERA_STATUS_UNAVAILABLE"),
                            self.clock(),
                        )
                    except ProbeFailure as exc:
                        value, deadline = self.unknown(pit, binding[2], str(exc)), self.clock()
                    except (ValidationError, ValueError, OverflowError):
                        value, deadline = (
                            self.unknown(pit, binding[2], "CAMERA_STATUS_BAD_RESPONSE"),
                            self.clock(),
                        )
                    if self.binding(pit) != binding:
                        self._cached = None
                        return self.unknown(pit, None, "CAMERA_STATUS_SOURCE_CHANGED")
                    steady = self.clock()
                    refresh = (
                        min(steady + REFRESH_SECONDS, deadline)
                        if value.validUntilAt
                        else steady + REFRESH_SECONDS
                    )
                    self._cached = CachedStatus(
                        binding, value, refresh, deadline, self.wall_clock()
                    )
                    return self.cached(binding) or self.unknown(
                        pit, binding[2], "CAMERA_STATUS_EXPIRED"
                    )
        except TimeoutError:
            return self.unknown(pit, binding[2], "CAMERA_STATUS_BUSY")

    async def observe(self, binding: Binding) -> CameraObservation:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=FETCH_SECONDS, follow_redirects=False, trust_env=False
            )
        # Internal status API carries no browser cookies, authorization or video bytes.
        self._client.cookies.clear()
        url = f"{binding[1]}/cameras/{quote(binding[2], safe='')}/status"
        async with self._client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}
        ) as response:
            if response.status_code != 200:
                raise ProbeFailure(
                    "CAMERA_STATUS_NOT_FOUND"
                    if response.status_code == 404
                    else "CAMERA_STATUS_UNAVAILABLE"
                )
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ProbeFailure("CAMERA_STATUS_BAD_RESPONSE")
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=MAX_BODY + 1):
                if len(content) + len(chunk) > MAX_BODY:
                    raise ProbeFailure("CAMERA_STATUS_BAD_RESPONSE")
                content.extend(chunk)
            return CameraObservation.model_validate_json(content)

    def interpret(
        self, binding: Binding, obs: CameraObservation, started: float
    ) -> tuple[CameraStatusV2, float]:
        wall, steady = self.wall_clock(), self.clock()
        if obs.camera_id != binding[2]:
            raise ProbeFailure("CAMERA_STATUS_IDENTITY_MISMATCH")
        if obs.assessed_at > wall + timedelta(seconds=1) or (
            obs.last_frame_received_at is not None
            and obs.last_frame_received_at > obs.assessed_at + timedelta(seconds=1)
        ):
            raise ProbeFailure("CAMERA_STATUS_CLOCK_UNTRUSTED")
        remaining = min(
            (obs.valid_until_at - wall).total_seconds(),
            (obs.valid_until_at - obs.assessed_at).total_seconds() - (steady - started),
        )
        deadline = steady + remaining
        if self._last is not None and self._last[0] == binding:
            if obs.assessed_at < self._last[1]:
                raise ProbeFailure("CAMERA_STATUS_CLOCK_UNTRUSTED")
            if obs.assessed_at == self._last[1]:
                deadline = min(deadline, self._last[2])
        if deadline <= steady:
            raise ProbeFailure("CAMERA_STATUS_EXPIRED")
        self._last = (binding, obs.assessed_at, deadline)
        value = CameraStatusV2(
            pitId=binding[0],
            cameraId=binding[2],
            state=obs.status.upper(),
            connectionState=obs.connection_state.upper(),
            statusBasis="ACCEPTED_JPEG_RECEIPT",
            computedAt=wall,
            assessedAt=obs.assessed_at,
            validUntilAt=wall + timedelta(seconds=deadline - steady),
            validForSeconds=deadline - steady,
            lastFrameReceivedAt=obs.last_frame_received_at,
            reasonCodes=[obs.reason_code],
        )
        return value, deadline


def camera_conditions(
    status: CameraStatusV2,
) -> tuple[list[ActiveHealthAlertV2], ActiveHealthCoverageV2]:
    coverage = ActiveHealthCoverageV2(
        componentKind="CAMERA",
        componentId=status.cameraId,
        state="UNSUPPORTED"
        if status.state == "UNSUPPORTED"
        else "UNKNOWN"
        if status.state == "UNKNOWN"
        else "CHECKED",
        reasonCodes=status.reasonCodes,
        evaluatedAt=status.computedAt if status.state != "UNSUPPORTED" else None,
        validUntilAt=status.validUntilAt,
        evidenceSource="MEDIA_CAMERA_STATUS" if status.statusBasis else None,
    )
    if status.state in ("HEALTHY", "UNSUPPORTED"):
        return [], coverage
    return [
        ActiveHealthAlertV2(
            id=f"health:camera:{status.pitId}:{status.cameraId}",
            type="device_error",
            severity="error" if status.state == "INTERRUPTED" else "warning",
            title="카메라 프레임 수신 중단"
            if status.state == "INTERRUPTED"
            else "카메라 수신 상태 확인 불가",
            componentKind="CAMERA",
            componentId=status.cameraId or "unknown",
            state=status.state,
            evaluatedAt=status.computedAt,
            measuredAt=status.lastFrameReceivedAt,
            evidenceSource="MEDIA_CAMERA_STATUS" if status.statusBasis else "STATUS_FRESHNESS",
            reasonCodes=status.reasonCodes,
            validUntilAt=status.validUntilAt,
        )
    ], coverage
