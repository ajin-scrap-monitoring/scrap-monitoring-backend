"""Synthetic receiver commits between profile reads; PostgreSQL snapshots retain one generation."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Measurement
from src.app.services import monitoring_v2_service as service
from src.app.services.edge_v2_service import accept_message
from tests import test_simulation_height as legacy
from tests.test_edge_world_height import world_context as world_context
from tests.test_edge_world_height import world_inputs
from tests.test_height_fill_pipeline import START
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_monitoring_v2 import login
from tests.test_sensor_angle_height import angle_context as angle_context


@pytest.mark.parametrize("http_snapshot", [False, True])
async def test_concurrent_receiver_does_not_empty_a_ready_profile(
    client, test_db_session, test_engine, world_context, monkeypatch, http_snapshot
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Concurrent receiver commits require PostgreSQL")
    db = test_db_session
    await legacy.prepare(client, db)
    for second in range(8):
        await legacy.frame(client, db, world_context, second, 50)
    await login(client)
    source = service.monitoring_source("pit-demo")
    now = START + timedelta(seconds=8)
    world_context["now"] = now
    _, next_body = world_inputs(90)
    next_body.update(message_id="concurrent-next-scan", generated_at=now.isoformat())
    for index, sensor in enumerate(next_body["sensors"]):
        sensor["scan"].update(
            scan_id=f"concurrent-next-{index}",
            sequence=8,
            acquired_at=now.isoformat(),
            acquired_monotonic_ns=str(9 * 10**9),
        )
    raw = service.raw_lidar_profiles
    committed = False

    async def commit_after_points(reading, selected_source, when=None):
        nonlocal committed
        page = await raw(reading, selected_source, when)
        if not committed:
            committed = True
            # The real receiver commits the new observation and its five-frame window
            # atomically. It must finish while the old reader is still open.
            async with AsyncSession(test_engine, expire_on_commit=False) as writer:
                await asyncio.wait_for(
                    accept_message(
                        writer,
                        Measurement.model_validate(next_body),
                        next_body,
                        get_settings().EDGE_V2_DEVICES[0],
                    ),
                    timeout=5,
                )
        return page

    monkeypatch.setattr(service, "raw_lidar_profiles", commit_after_points)
    if http_snapshot:
        response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        profiles = response.json()["profiles"]
    else:
        # Control: the unchanged low-level reader under READ COMMITTED mixes generations.
        await db.rollback()
        page = await service.lidar_profiles(db, source, now)
        profiles = [p.model_dump(mode="json") for p in page.profiles]
    assert committed
    for profile in profiles:
        assert profile["average"] == 50
        assert profile["filteredAverage"] == (50 if http_snapshot else None)
        assert profile["measuredAt"] == (START + timedelta(seconds=7)).isoformat().replace(
            "+00:00", "Z"
        )
        assert all(s["height"] == 50 for s in profile["samples"])
    async with AsyncSession(test_engine) as observer:
        latest = await observer.get(EdgeV2Latest, (source.site_id, source.edge_id, "measurement"))
        assert latest.message_id == next_body["message_id"]


async def test_profile_snapshot_keeps_real_missing_heights_null(
    client, test_db_session, world_context, monkeypatch
):
    db = test_db_session
    await legacy.prepare(client, db)
    for second in range(5):
        await legacy.frame(client, db, world_context, second, 50)
    await login(client)

    def missing(height=50):
        config, measurement = world_inputs(height)
        for sensor in measurement["sensors"]:
            for point in sensor["scan"]["points"]:
                point["position_mm"] = None
        return config, measurement

    monkeypatch.setattr(legacy, "raw_inputs", missing)
    await legacy.frame(client, db, world_context, 5)
    response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
    assert response.status_code == 200
    for profile in response.json()["profiles"]:
        assert profile["average"] is None and profile["filteredAverage"] is None
        assert all(s["height"] is None and not s["valid"] for s in profile["samples"])


@pytest.mark.parametrize("cancel", [False, True])
async def test_profile_snapshot_releases_connection_on_failure(
    test_db_session, test_engine, world_context, monkeypatch, cancel
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Snapshot connection lifecycle requires PostgreSQL")
    caller = test_db_session
    await caller.execute(text("SELECT 1"))
    checked_out = test_engine.pool.checkedout()

    async def fail(reading, source, now=None):
        assert reading is not caller
        assert await reading.scalar(text("SHOW transaction_read_only")) == "on"
        assert await reading.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("synthetic read failure")

    monkeypatch.setattr(service, "lidar_profiles", fail)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await service.lidar_profile_snapshot(caller, service.monitoring_source("pit-demo"), START)
    assert test_engine.pool.checkedout() == checked_out
    assert caller.in_transaction()
    assert await caller.scalar(text("SELECT 1")) == 1
