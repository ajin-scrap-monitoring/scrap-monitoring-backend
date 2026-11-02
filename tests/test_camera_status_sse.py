"""Opt-in camera events share bounded reads without blocking measurement delivery."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_camera_status import camera as camera
from test_camera_status import observation
from test_monitoring_v2 import assessment, login, monitoring_config, send, status  # noqa: F401

from src.app.api import monitoring_v2 as route
from src.app.core.config import get_settings
from src.app.main import create_app
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.services import camera_status_stream as streaming
from src.app.services.monitoring_v2_service import monitoring_source
from src.app.services.session_service import session_service

URL = "/api/v2/monitoring/events"


class StreamRequest:
    def __init__(self, app):
        self.app = app
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


@pytest.fixture
def fast_ticks(monkeypatch):
    original = asyncio.sleep

    async def tick(seconds):
        await original(0)

    monkeypatch.setattr(route.asyncio, "sleep", tick)
    return original


async def open_stream(client, db, *, enabled=True):
    session_id, session = await login(client)
    request = StreamRequest(client._transport.app)
    response = await route.events(
        request,
        session,
        monitoring_source("pit-demo"),
        db,
        session_id,
        include_camera_status=enabled,
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    return response.body_iterator, session_id, request


async def next_camera(generator):
    for _ in range(8):
        frame = await anext(generator)
        if frame.startswith("event: monitoring.camera-status\n"):
            return json.loads(frame.split("data: ", 1)[1])
    pytest.fail("Camera event was not delivered")


async def test_default_stream_retains_snapshot_only_and_never_reads_camera(
    client, test_db_session, camera, fast_ticks
):
    stream, _, _ = await open_stream(client, test_db_session, enabled=False)
    try:
        assert (await anext(stream)).startswith("event: monitoring.snapshot\n")
        assert await anext(stream) == ": keepalive\n\n"
        assert camera.calls == 0 and not test_db_session.in_transaction()
    finally:
        await stream.aclose()


async def test_opt_in_events_reuse_get_contract_and_never_write_business_records(
    client, test_db_session, camera, fast_ticks
):
    stream, _, _ = await open_stream(client, test_db_session)
    try:
        assert (await anext(stream)).startswith("event: monitoring.snapshot\n")
        body = await next_camera(stream)
        assert body["state"] == "HEALTHY" and body["statusBasis"] == "ACCEPTED_JPEG_RECEIPT"
        assert body["pitId"] == "pit-demo" and body["cameraId"] == "camera-main"
        assert 0 < body["validForSeconds"] <= 5
        assert "lastFrameReceivedAt" in body and "snapshotId" not in body
        regular = await client.get(
            "/api/v2/monitoring/camera-status", params={"pit_id": "pit-demo"}
        )
        assert regular.status_code == 200 and regular.json() == body
        assert camera.calls == 1
        assert not test_db_session.in_transaction()
        for model in [AlertLog, MediaClipJob, EdgeV2Latest]:
            assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        await stream.aclose()


async def test_slow_camera_does_not_delay_snapshot_and_cancellation_releases_read(
    client, test_db_session, camera, fast_ticks
):
    entered, finished = asyncio.Event(), asyncio.Event()
    gate = asyncio.Event()

    async def blocked():
        entered.set()
        try:
            await gate.wait()
        finally:
            finished.set()

    camera.hook = blocked
    stream, _, _ = await open_stream(client, test_db_session)
    assert (await anext(stream)).startswith("event: monitoring.snapshot\n")
    await asyncio.wait_for(entered.wait(), 1)
    assert not test_db_session.in_transaction()
    await send(client, status())
    await send(client, assessment())
    updated = await anext(stream)
    assert updated.startswith("event: monitoring.snapshot\n") and '"loadPercent":72.5' in updated
    assert not finished.is_set() and not test_db_session.in_transaction()
    await stream.aclose()
    assert finished.is_set() and not camera.service._lock.locked()
    camera.hook = None
    assert (await camera.service.get("pit-demo")).state == "HEALTHY"


async def test_multiple_streams_share_one_probe_and_get_cache(
    client, test_db_session, camera, fast_ticks
):
    first, _, _ = await open_stream(client, test_db_session)
    second, _, _ = await open_stream(client, test_db_session)
    try:
        await anext(first)
        await anext(second)
        assert (await next_camera(first))["state"] == "HEALTHY"
        assert (await next_camera(second))["state"] == "HEALTHY"
        assert camera.calls == 1
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("state", ["healthy", "interrupted"])
async def test_unchanged_state_is_renewed_without_waiting_for_snapshot_changes(
    client, test_db_session, camera, fast_ticks, monkeypatch, state
):
    if state == "interrupted":
        camera.body.update(status="interrupted", reason_code="FRAME_STALE")

    async def due(seconds):
        camera.steady += seconds
        camera.wall += timedelta(seconds=seconds)
        fresh = observation(camera.wall)
        camera.body.update(assessed_at=fresh["assessed_at"], valid_until_at=fresh["valid_until_at"])
        await fast_ticks(0)

    monkeypatch.setattr(streaming, "sleep", due)
    stream, _, _ = await open_stream(client, test_db_session)
    try:
        await anext(stream)
        first = await next_camera(stream)
        second = await next_camera(stream)
        assert first["state"] == second["state"] == state.upper()
        assert first["validUntilAt"] < second["validUntilAt"]
        assert first["lastFrameReceivedAt"] == second["lastFrameReceivedAt"]
        assert camera.calls == 2
    finally:
        await stream.aclose()


@pytest.mark.parametrize("failure", [503, "exception"])
async def test_failed_camera_is_unknown_without_ending_measurement_stream(
    client, test_db_session, camera, fast_ticks, monkeypatch, failure
):
    if failure == "exception":

        async def fail(pit):
            raise RuntimeError("private upstream diagnostic")

        monkeypatch.setattr(camera.service, "get", fail)
    else:
        camera.status = failure
    stream, _, _ = await open_stream(client, test_db_session)
    try:
        await anext(stream)
        body = await next_camera(stream)
        assert body["state"] == "UNKNOWN" and "private" not in json.dumps(body)
        assert await anext(stream) == ": keepalive\n\n"
    finally:
        await stream.aclose()


async def test_delayed_completed_read_cannot_refresh_expired_evidence(
    client, test_db_session, camera, fast_ticks
):
    stream, _, _ = await open_stream(client, test_db_session)
    try:
        await anext(stream)
        await fast_ticks(0.01)
        camera.steady += 6
        camera.wall += timedelta(seconds=6)
        body = await next_camera(stream)
        assert body["state"] == "UNKNOWN" and body["validForSeconds"] is None
        assert body["reasonCodes"] == ["CAMERA_STATUS_EXPIRED"]
    finally:
        await stream.aclose()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("MEDIA_CAMERA_ID", "replacement"),
        ("MEDIA_DAILY_SERVICE_URL", "http://replacement.test/api/v2"),
    ],
)
async def test_binding_change_does_not_publish_previous_camera(camera, monkeypatch, setting, value):
    reading = await streaming.read_camera_status(camera.service, "pit-demo")
    monkeypatch.setattr(get_settings(), setting, value)
    body = streaming.delivery_status(camera.service, reading)
    assert body.state == "UNKNOWN" and body.reasonCodes == ["CAMERA_STATUS_SOURCE_CHANGED"]


async def test_delivery_expiry_uses_wall_and_monotonic_time_and_preserves_remaining_lease(camera):
    reading = await streaming.read_camera_status(camera.service, "pit-demo")
    camera.steady += 1
    camera.wall += timedelta(seconds=1)
    assert streaming.delivery_status(camera.service, reading).validForSeconds == 3
    camera.steady += 4
    assert streaming.delivery_status(camera.service, reading).state == "UNKNOWN"


@pytest.mark.parametrize("clock", ["wall", "steady"])
async def test_delivery_rejects_a_clock_moving_backwards(camera, clock):
    reading = await streaming.read_camera_status(camera.service, "pit-demo")
    if clock == "wall":
        camera.wall -= timedelta(seconds=1)
    else:
        camera.steady -= 1
    value = streaming.delivery_status(camera.service, reading)
    assert value.state == "UNKNOWN" and value.reasonCodes == ["CAMERA_STATUS_CLOCK_UNTRUSTED"]


async def test_unconfigured_camera_is_unsupported_without_upstream_requests(
    client, test_db_session, camera, fast_ticks, monkeypatch
):
    monkeypatch.setattr(get_settings(), "MEDIA_PIT_ID", "another-pit")
    stream, _, _ = await open_stream(client, test_db_session)
    try:
        await anext(stream)
        value = await next_camera(stream)
        assert value["state"] == "UNSUPPORTED" and camera.calls == 0
    finally:
        await stream.aclose()


async def test_logout_and_disconnect_stop_camera_reads(client, test_db_session, camera, fast_ticks):
    stream, session_id, request = await open_stream(client, test_db_session)
    await anext(stream)
    await next_camera(stream)
    await session_service.delete_session(test_db_session, session_id)
    assert await anext(stream) == "event: session.expired\ndata: {}\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not test_db_session.in_transaction()
    assert not camera.service._lock.locked()

    stream, _, request = await open_stream(client, test_db_session)
    await anext(stream)
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not test_db_session.in_transaction() and not camera.service._lock.locked()


async def test_session_and_query_validation_happen_before_camera_probe(client, camera):
    assert (
        await client.get(URL, params={"pit_id": "pit-demo", "include_camera_status": "true"})
    ).status_code == 401
    await login(client)
    assert (
        await client.get(URL, params={"pit_id": "pit-demo", "include_camera_status": "invalid"})
    ).status_code == 422
    assert (
        await client.get(URL, params={"pit_id": "unmapped", "include_camera_status": "true"})
    ).status_code == 404
    assert camera.calls == 0


def test_openapi_declares_camera_delivery_as_opt_in():
    params = create_app().openapi()["paths"][URL]["get"]["parameters"]
    include = next(value for value in params if value["name"] == "include_camera_status")
    assert include["schema"]["type"] == "boolean" and include["schema"]["default"] is False
