"""Synthetic minute history retains true metadata for the selected scalar slot."""

import copy
from datetime import timedelta

import pytest

from src.app.models.edge_v2_projection import EdgeV2Minute
from src.app.services.load_history_v2_service import load_history
from src.app.services.monitoring_v2_service import monitoring_source
from tests.test_compact_lidar_storage import START, healthy, sample, send
from tests.test_compact_lidar_storage import compact_config as compact_config
from tests.test_monitoring_v2 import assessment


async def test_partial_minute_keeps_selected_actual_times_after_later_reports(
    client, test_db_session, compact_config
):
    await healthy(client, compact_config)
    for second in range(10):
        await sample(client, test_db_session, compact_config, second, 70 + second)
    history = await load_history(
        test_db_session,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(seconds=7),
        60,
        START + timedelta(seconds=10),
    )
    point = history.samples[0]
    assert point.valuePercent == 74
    assert point.generatedAt == point.observedAt == START + timedelta(seconds=6)
    assert point.assessmentId == "state-6"
    assert point.valid and point.dataState == "AVAILABLE"
    assert "COMPACT_SUBMINUTE_METADATA_NOT_RETAINED" not in point.reasonCodes


@pytest.mark.parametrize("begin,end,chosen,value", [(0, 6.1, 5.2, 73), (5.3, 7.1, 6.2, 74)])
async def test_precise_metadata_respects_both_half_open_microsecond_boundaries(
    client, test_db_session, compact_config, begin, end, chosen, value
):
    await healthy(client, compact_config)
    for index in range(10):
        await sample(client, test_db_session, compact_config, index + 0.2, 70 + index)
    history = await load_history(
        test_db_session,
        monitoring_source("pit-demo"),
        START + timedelta(seconds=begin),
        START + timedelta(seconds=end),
        60,
        START + timedelta(seconds=10),
    )
    point = history.samples[0]
    assert point.valuePercent == value
    assert point.generatedAt == point.observedAt == START + timedelta(seconds=chosen)
    assert point.bucketStartAt <= point.generatedAt < point.bucketEndAt


async def test_same_second_replacement_and_replay_keep_scalar_and_metadata_together(
    client, test_db_session, compact_config
):
    db = test_db_session
    await healthy(client, compact_config)
    for second in range(5):
        await sample(client, db, compact_config, second, 70)
    when = START + timedelta(seconds=4.8)
    payload = assessment("new-slot-value", 0.8, when)
    assert (await send(client, payload, compact_config)).status_code == 201
    row = await db.get(EdgeV2Minute, ("site-demo", "edge-demo", START))
    key = str(int(when.timestamp()))
    assert row.payload["metadata"][key]["generatedAt"] == when.isoformat()
    assert row.payload["metadata"][key]["assessmentId"] == "new-slot-value"
    assert set(row.payload["metadata"]) == set(row.payload["values"])
    before = copy.deepcopy(row.payload)
    assert (await send(client, payload, compact_config)).json()["duplicate"]
    assert row.payload == before
    history = await load_history(
        db, monitoring_source("pit-demo"), START, START + timedelta(seconds=5), 60, when
    )
    assert history.samples[0].valuePercent == row.payload["values"][key]
    assert history.samples[0].generatedAt == when


async def test_legacy_slot_without_metadata_keeps_nullable_contract(
    client, test_db_session, compact_config
):
    db = test_db_session
    await healthy(client, compact_config)
    for second in range(10):
        await sample(client, db, compact_config, second, 70 + second)
    row = await db.get(EdgeV2Minute, ("site-demo", "edge-demo", START))
    payload = copy.deepcopy(row.payload)
    payload.get("metadata", {}).pop(str(int((START + timedelta(seconds=6)).timestamp())), None)
    row.payload = payload
    await db.commit()
    history = await load_history(
        db,
        monitoring_source("pit-demo"),
        START,
        START + timedelta(seconds=7),
        60,
        START + timedelta(seconds=10),
    )
    point = history.samples[0]
    assert point.valuePercent == 74 and point.valid
    assert point.generatedAt is None and point.observedAt is None
    assert point.reasonCodes == ["COMPACT_SUBMINUTE_METADATA_NOT_RETAINED"]


async def test_metadata_is_bounded_to_the_same_sixty_scalar_keys(
    client, test_db_session, compact_config
):
    await healthy(client, compact_config)
    for second in range(65):
        await sample(client, test_db_session, compact_config, second, 70)
    for bucket in [START, START + timedelta(minutes=1)]:
        row = await test_db_session.get(EdgeV2Minute, ("site-demo", "edge-demo", bucket))
        assert len(row.payload["metadata"]) == len(row.payload["values"]) <= 60
        assert all(
            set(m)
            == {
                "generatedAt",
                "observedAt",
                "assessmentId",
                "observationId",
                "dataState",
                "reasonCodes",
            }
            for m in row.payload["metadata"].values()
        )
