"""Real PostgreSQL snapshots under an invented concurrent projection commit."""

import copy

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Filter, EdgeV2Latest
from src.app.services import collection_detection_service as detection
from src.app.services.monitoring_v2_service import monitoring_source
from tests.test_collection_detection import detector_frame
from tests.test_collection_detection import enabled_detector as enabled_detector
from tests.test_height_fill_pipeline import START
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_sensor_angle_height import angle_context as angle_context
from tests.test_simulation_height import prepare


@pytest.mark.parametrize("consistent", [False, True])
async def test_receiver_commit_cannot_mix_observation_and_upper_window(
    client, test_db_session, test_engine, enabled_detector, monkeypatch, consistent
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Concurrent READ COMMITTED/REPEATABLE READ requires --postgresql")
    db = test_db_session
    await prepare(client, db)
    for second in range(8):
        await detector_frame(client, db, enabled_detector, second, 80)
    source = monitoring_source("pit-demo")
    original = detection.collection_height
    old_id = None

    async def commit_between_reads(reading, selected_source, observation, representative, now):
        nonlocal old_id
        old_id = observation.message_id
        # The receiver commits both latest projections and the filter together. No
        # receiver row lock is held by the reader, so this commit must not block.
        async with AsyncSession(test_engine, expire_on_commit=False) as writer:
            for kind in ("height_window", "height_fill"):
                row = await writer.get(EdgeV2Latest, (source.site_id, source.edge_id, kind))
                row.message_id = "public-next-generation"
                if kind == "height_fill":
                    payload = copy.deepcopy(row.payload)
                    payload["message_id"] = row.message_id
                    row.payload = payload
            filtered = await writer.get(EdgeV2Filter, (source.site_id, source.edge_id))
            payload = copy.deepcopy(filtered.payload)
            payload["message_id"] = "public-next-generation"
            filtered.payload = payload
            await writer.commit()
        return await original(reading, selected_source, observation, representative, now)

    monkeypatch.setattr(detection, "collection_height", commit_between_reads)
    read = detection.collection_input if consistent else detection.read_collection_input
    snapshot, _, _, upper = await read(db, source, enabled_detector["now"])
    assert snapshot.fillObservation.message_id == old_id
    assert (upper is not None) is consistent
    assert snapshot.dataState == "FRESH" and snapshot.summary.filterState == "READY"
    assert old_id != "public-next-generation"
    assert enabled_detector["now"] >= START
