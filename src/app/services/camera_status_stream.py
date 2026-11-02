"""Bounded asynchronous camera reads and evidence expiry at SSE delivery time."""

from asyncio import sleep
from dataclasses import dataclass

from src.app.schemas.camera_status import CameraStatusV2
from src.app.services.camera_status_service import Binding, CameraStatusService


@dataclass
class CameraStreamRead:
    pit_id: str
    binding: Binding | None
    status: CameraStatusV2
    received_at: float


async def read_camera_status(
    service: CameraStatusService, pit_id: str, *, not_before: float | None = None
) -> CameraStreamRead:
    if not_before is not None:
        await sleep(max(0.0, not_before - service.clock()))
    binding = service.binding(pit_id)
    try:
        status = await service.get(pit_id)
    except Exception:
        # Camera failures cannot terminate the independent measurement stream.
        # Cancellation propagates (CancelledError is a BaseException).
        status = service.unknown(pit_id, None, "CAMERA_STATUS_UNAVAILABLE")
    return CameraStreamRead(pit_id, binding, status, service.clock())


def delivery_status(service: CameraStatusService, reading: CameraStreamRead) -> CameraStatusV2:
    """A slow consumer must not renew a completed read's lease by receiving it late."""
    status = reading.status
    if service.binding(reading.pit_id) != reading.binding or status.pitId != reading.pit_id:
        return service.unknown(reading.pit_id, None, "CAMERA_STATUS_SOURCE_CHANGED")
    wall, steady = service.wall_clock(), service.clock()
    if wall < status.computedAt or steady < reading.received_at:
        return service.unknown(reading.pit_id, status.cameraId, "CAMERA_STATUS_CLOCK_UNTRUSTED")
    if status.validUntilAt is not None and status.validForSeconds is not None:
        remaining = min(
            status.validForSeconds - (steady - reading.received_at),
            (status.validUntilAt - wall).total_seconds(),
        )
        if remaining <= 0:
            return service.unknown(reading.pit_id, status.cameraId, "CAMERA_STATUS_EXPIRED")
        return status.model_copy(update={"computedAt": wall, "validForSeconds": remaining})
    if status.state in ("HEALTHY", "INTERRUPTED"):
        return service.unknown(reading.pit_id, status.cameraId, "CAMERA_STATUS_EXPIRED")
    return status.model_copy(update={"computedAt": wall})
