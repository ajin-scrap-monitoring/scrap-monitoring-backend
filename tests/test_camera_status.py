"""Camera receipt evidence stays fresh, isolated, bounded and read-only."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from test_monitoring_v2 import NOW, login
from test_monitoring_v2 import monitoring_config as monitoring_config

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob

URL = "/api/v2/monitoring/camera-status"
ACTIVE = "/api/v2/monitoring/active-alerts"
PARAMS = {"pit_id": "pit-demo"}


def observation(when=NOW):
    return {
        "camera_id": "camera-main",
        "status_basis": "accepted_jpeg_receipt",
        "status": "healthy",
        "connection_state": "connected",
        "last_frame_received_at": (when - timedelta(seconds=1)).isoformat(),
        "assessed_at": when.isoformat(),
        "valid_until_at": (when + timedelta(seconds=4)).isoformat(),
        "reason_code": "FRAME_RECEIVED",
        "freshness_seconds": 5,
    }


@pytest.fixture
async def camera(client, monkeypatch, monitoring_config):
    settings = get_settings()
    for key, value in {
        "MEDIA_PIT_ID": "pit-demo",
        "MEDIA_CAMERA_ID": "camera-main",
        "MEDIA_SERVICE_URL": None,
        "MEDIA_API_TOKEN": None,
        "MEDIA_DAILY_SERVICE_URL": "http://camera.test/api/v2",
    }.items():
        monkeypatch.setattr(settings, key, value)
    service = client._transport.app.state.camera_status
    fake = SimpleNamespace(
        body=observation(),
        wall=NOW,
        steady=1000.0,
        calls=0,
        status=200,
        raw=None,
        encoding=None,
        hook=None,
    )
    service.wall_clock = lambda: fake.wall
    service.clock = lambda: fake.steady
    fake.service = service

    async def handle(request):
        fake.calls += 1
        assert request.method == "GET" and request.url.host == "camera.test"
        assert request.url.path.startswith("/api/v1/cameras/")
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if fake.hook is not None:
            await fake.hook()
        headers = {"Content-Encoding": fake.encoding} if fake.encoding else {}
        if fake.raw is not None:
            return httpx.Response(fake.status, content=fake.raw, headers=headers)
        return httpx.Response(fake.status, json=fake.body, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        service._client = upstream
        yield fake
        await service.close()


async def test_camera_auth_scope_cache_and_no_business_writes(client, camera, test_db_session):
    assert (await client.get(URL, params=PARAMS)).status_code == 401 and camera.calls == 0
    await login(client)
    plain = await client.get(ACTIVE, params=PARAMS)
    assert plain.status_code == 200 and camera.calls == 0
    response = await client.get(URL, params=PARAMS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["state"] == "HEALTHY" and body["statusBasis"] == "ACCEPTED_JPEG_RECEIPT"
    assert 0 < body["validForSeconds"] <= 5 and body["validUntilAt"].endswith("Z")
    active = (await client.get(ACTIVE, params={**PARAMS, "scope": "ALL"})).json()
    coverage = next(x for x in active["coverage"] if x["componentKind"] == "CAMERA")
    assert coverage["state"] == "CHECKED" and "validUntilAt" in coverage
    assert not any(x.get("componentKind") == "CAMERA" for x in active["items"])
    assert active["evaluationState"] == plain.json()["evaluationState"]
    assert camera.calls == 1
    assert (await client.get(URL, params={"pit_id": "not-configured"})).status_code == 404
    for model in [EdgeV2Latest, AlertLog, MediaClipJob]:
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "state,connection,reason,frame",
    [
        ("healthy", "connected", "FRAME_RECEIVED", True),
        ("unknown", "unknown", "NO_OBSERVATION", False),
        ("unknown", "connected", "AWAITING_FRAME", False),
        ("interrupted", "connected", "FRAME_STALE", True),
        ("interrupted", "connected", "FRAME_TIMEOUT", False),
        ("interrupted", "disconnected", "CONNECTION_CLOSED", True),
        ("interrupted", "disconnected", "INVALID_JPEG", False),
    ],
)
async def test_camera_states_keep_their_source_and_do_not_invent_outage_starts(
    client, camera, state, connection, reason, frame
):
    camera.body.update(status=state, connection_state=connection, reason_code=reason)
    if not frame:
        camera.body["last_frame_received_at"] = None
    await login(client)
    body = (await client.get(ACTIVE, params={**PARAMS, "scope": "ALL"})).json()
    coverage = next(x for x in body["coverage"] if x["componentKind"] == "CAMERA")
    assert coverage["state"] == ("UNKNOWN" if state == "unknown" else "CHECKED")
    items = [x for x in body["items"] if x.get("componentKind") == "CAMERA"]
    if state == "healthy":
        assert not items
    else:
        assert len(items) == 1 and items[0]["state"] == state.upper()
        assert items[0]["startedAt"] is None and items[0]["startBasis"] == "UNKNOWN"
        assert items[0]["evidenceSource"] == "MEDIA_CAMERA_STATUS"
        assert items[0]["reasonCodes"] == [reason]
        assert items[0]["severity"] == ("error" if state == "interrupted" else "warning")


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"camera_id": "other"}, "CAMERA_STATUS_IDENTITY_MISMATCH"),
        ({"status_basis": "hardware_test"}, "CAMERA_STATUS_BAD_RESPONSE"),
        ({"connection_state": "disconnected"}, "CAMERA_STATUS_BAD_RESPONSE"),
        ({"freshness_seconds": 6}, "CAMERA_STATUS_BAD_RESPONSE"),
        (
            {"valid_until_at": (NOW + timedelta(seconds=6)).isoformat()},
            "CAMERA_STATUS_BAD_RESPONSE",
        ),
        ({"assessed_at": NOW.replace(tzinfo=None).isoformat()}, "CAMERA_STATUS_BAD_RESPONSE"),
        (
            {
                "assessed_at": (NOW + timedelta(seconds=10)).isoformat(),
                "valid_until_at": (NOW + timedelta(seconds=14)).isoformat(),
            },
            "CAMERA_STATUS_CLOCK_UNTRUSTED",
        ),
        (
            {
                "assessed_at": (NOW - timedelta(seconds=10)).isoformat(),
                "last_frame_received_at": (NOW - timedelta(seconds=11)).isoformat(),
                "valid_until_at": (NOW - timedelta(seconds=6)).isoformat(),
            },
            "CAMERA_STATUS_EXPIRED",
        ),
    ],
)
async def test_bad_or_expired_camera_evidence_never_becomes_healthy(client, camera, patch, reason):
    camera.body.update(patch)
    await login(client)
    response = await client.get(URL, params=PARAMS)
    assert response.status_code == 200
    assert response.json()["state"] == "UNKNOWN" and response.json()["reasonCodes"] == [reason]
    assert response.json()["validUntilAt"] is None


@pytest.mark.parametrize(
    "status,raw,encoding",
    [
        (404, None, None),
        (503, None, None),
        (302, None, None),
        (200, b"private invalid JSON", None),
        (200, b"x" * 17000, None),
        (200, b"compressed", "gzip"),
    ],
)
async def test_probe_errors_are_bounded_unknown_and_do_not_break_other_alerts(
    client, camera, status, raw, encoding
):
    camera.status, camera.raw, camera.encoding = status, raw, encoding
    await login(client)
    response = await client.get(ACTIVE, params={**PARAMS, "scope": "ALL"})
    assert response.status_code == 200 and "private invalid" not in response.text
    camera_item = next(x for x in response.json()["items"] if x.get("componentKind") == "CAMERA")
    assert camera_item["state"] == "UNKNOWN" and camera_item["evidenceSource"] == "STATUS_FRESHNESS"
    assert "validUntilAt" not in camera_item
    await client.get(URL, params=PARAMS)
    assert camera.calls == 1  # Failures are briefly cached too.


async def test_frozen_wall_clock_and_replayed_reply_cannot_extend_health(camera):
    service = camera.service
    assert (await service.get("pit-demo")).state == "HEALTHY"
    camera.steady += 3
    second = await service.get("pit-demo")
    assert second.state == "HEALTHY" and second.validForSeconds == 1
    camera.steady += 2
    expired = await service.get("pit-demo")
    assert expired.state == "UNKNOWN" and expired.reasonCodes == ["CAMERA_STATUS_EXPIRED"]


async def test_clock_rollback_and_source_change_discard_old_normal(camera, monkeypatch):
    service = camera.service
    assert (await service.get("pit-demo")).state == "HEALTHY"
    camera.wall -= timedelta(hours=1)
    camera.steady += 3
    assert (await service.get("pit-demo")).state == "UNKNOWN"
    monkeypatch.setattr(get_settings(), "MEDIA_CAMERA_ID", "new-camera")
    camera.body = observation(camera.wall)
    camera.body["camera_id"] = "new-camera"
    assert (await service.get("pit-demo")).cameraId == "new-camera"
    monkeypatch.setattr(get_settings(), "MEDIA_DAILY_SERVICE_URL", None)
    assert (await service.get("pit-demo")).state == "UNSUPPORTED"


async def test_response_delay_consumes_the_source_validity_budget(camera):
    async def delayed():
        camera.steady += 5

    camera.hook = delayed
    assert (await camera.service.get("pit-demo")).reasonCodes == ["CAMERA_STATUS_EXPIRED"]


async def test_single_flight_copies_and_cancellation_release_resources(camera):
    async def delay():
        await asyncio.sleep(0.01)

    camera.hook = delay
    values = await asyncio.gather(*(camera.service.get("pit-demo") for _ in range(20)))
    assert camera.calls == 1 and all(x.state == "HEALTHY" for x in values)
    values[0].reasonCodes.append("SYNTHETIC_MUTATION")
    assert (await camera.service.get("pit-demo")).reasonCodes == ["FRAME_RECEIVED"]
    camera.steady += 3
    camera.wall += timedelta(seconds=3)
    camera.body = observation(camera.wall)
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.sleep(5)

    camera.hook = blocked
    task = asyncio.create_task(camera.service.get("pit-demo"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not camera.service._lock.locked()
    camera.hook = None
    assert (await camera.service.get("pit-demo")).state == "HEALTHY"


async def test_probe_timeout_is_unknown_and_does_not_keep_a_normal_cache(camera):
    async def blocked():
        await asyncio.sleep(5)

    camera.hook = blocked
    result = await camera.service.get("pit-demo")
    assert result.state == "UNKNOWN" and result.reasonCodes == ["CAMERA_STATUS_TIMEOUT"]
    assert (await camera.service.get("pit-demo")).state == "UNKNOWN" and camera.calls == 1


async def test_configuration_change_during_request_is_not_published(camera, monkeypatch):
    async def changed():
        monkeypatch.setattr(get_settings(), "MEDIA_CAMERA_ID", "changed-camera")

    camera.hook = changed
    result = await camera.service.get("pit-demo")
    assert result.state == "UNKNOWN" and result.reasonCodes == ["CAMERA_STATUS_SOURCE_CHANGED"]
    assert camera.service._cached is None
