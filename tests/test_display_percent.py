"""JSON rounding stays consistent without moving stored inputs or threshold decisions."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.app.api.monitoring_v2 import events
from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_operations import EdgeV2Operation
from src.app.schemas.load_history_v2 import HistorySampleV2
from src.app.schemas.monitoring_v2 import LidarProfilePageV2, LidarProfileV2, MonitoringSummaryV2
from src.app.services.edge_v2_operations import process_source
from src.app.services.monitoring_v2_service import build_snapshot, monitoring_source
from tests.test_monitoring_v2 import NOW, ConnectedRequest, assessment, login, send, status
from tests.test_monitoring_v2 import monitoring_config as monitoring_config


@pytest.mark.parametrize(
    "original,expected",
    [
        (57.74056871295116, 57.74),
        (57.745, 57.75),
        (1.005, 1.01),
        (57.735, 57.74),
        (74.9999, 75.0),
        (99.999, 100.0),
        (0.0, 0.0),
        (100.0, 100.0),
        (None, None),
    ],
)
def test_json_display_percent_keeps_python_precision_and_numeric_nulls(original, expected):
    summary = MonitoringSummaryV2(
        loadPercent=original,
        rawLoadPercent=original,
        warningThresholdPercent=75,
        collectionThresholdPercent=90,
        recoveryThresholdPercent=85,
    )
    assert summary.loadPercent == original
    assert summary.model_dump()["loadPercent"] == original
    assert summary.model_dump(mode="json")["loadPercent"] == expected
    assert json.loads(summary.model_dump_json())["rawLoadPercent"] == expected
    history = HistorySampleV2(
        bucketStartAt=NOW,
        bucketEndAt=NOW + timedelta(minutes=1),
        valuePercent=original,
        averagePercent=original,
        minimumPercent=original,
        maximumPercent=original,
    )
    for field in ("valuePercent", "averagePercent", "minimumPercent", "maximumPercent"):
        assert getattr(history, field) == original
        assert history.model_dump(mode="json")[field] == expected
    profile = LidarProfileV2(
        sensorId="lidar-a", referenceDistanceCm=1000, sectionFillPercent=original, average=original
    )
    page = LidarProfilePageV2(pitId="pit-demo", profiles=[profile], meanSectionFillPercent=original)
    assert page.model_dump(mode="json")["meanSectionFillPercent"] == expected
    assert page.model_dump(mode="json")["profiles"][0]["sectionFillPercent"] == expected
    assert page.model_dump(mode="json")["profiles"][0]["average"] == original  # cm are unchanged.


async def test_snapshot_sse_history_round_only_json_and_preserve_receipt(client, test_db_session):
    db = test_db_session
    await send(client, status())
    raw = assessment(ratio=0.57745)
    await send(client, raw)
    session_id, session = await login(client)
    snapshot = await client.get("/api/v2/monitoring/snapshot?pit_id=pit-demo")
    assert snapshot.status_code == 200
    assert snapshot.json()["summary"]["loadPercent"] == 57.75
    assert snapshot.json()["summary"]["rawLoadPercent"] == 57.75
    assert snapshot.json()["assessment"]["fill_estimate"]["ratio"] == 0.57745
    response = await events(
        ConnectedRequest(), session, monitoring_source("pit-demo"), db, session_id
    )
    generator = response.body_iterator
    try:
        initial = await anext(generator)
        data = json.loads(initial.split("data: ", 1)[1].strip())
        assert data["summary"]["loadPercent"] == 57.75
    finally:
        await generator.aclose()
    history = await client.get(
        "/api/v2/monitoring/load-history",
        params={
            "pit_id": "pit-demo",
            "from": (NOW - timedelta(seconds=1)).isoformat(),
            "to": (NOW + timedelta(seconds=1)).isoformat(),
            "interval": 60,
        },
    )
    assert history.status_code == 200
    assert next(s for s in history.json()["samples"] if s["valid"])["valuePercent"] == 57.75
    stored = await db.scalar(
        select(EdgeV2Message).where(EdgeV2Message.message_id == raw["message_id"])
    )
    assert stored.payload == raw
    internal = await build_snapshot(db, monitoring_source("pit-demo"), NOW)
    assert internal.summary.loadPercent == 57.745


async def test_rounded_display_does_not_cross_operational_threshold(
    client, test_db_session, monkeypatch
):
    monkeypatch.setenv("EDGE_V2_OPERATIONS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        await send(client, status())
        await send(client, assessment(ratio=0.749999))
        await process_source(test_db_session, monitoring_source("pit-demo"), NOW)
        snapshot = await build_snapshot(test_db_session, monitoring_source("pit-demo"), NOW)
        assert snapshot.summary.loadPercent == 74.9999
        assert snapshot.model_dump(mode="json")["summary"]["loadPercent"] == 75.0
        assert snapshot.operations.effectiveState == "NORMAL"
        row = await test_db_session.get(EdgeV2Operation, "pit-demo")
        assert row.state == "NORMAL"
    finally:
        get_settings.cache_clear()


def test_display_percent_response_schema_keeps_number_null_and_percent_bounds():
    from src.app.main import create_app

    schemas = create_app().openapi()["components"]["schemas"]
    field = schemas["MonitoringSummaryV2"]["properties"]["loadPercent"]
    assert field["anyOf"] == [
        {"type": "number", "maximum": 100.0, "minimum": 0.0},
        {"type": "null"},
    ]
