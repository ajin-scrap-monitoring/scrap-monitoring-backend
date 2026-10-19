"""Confirmed completions stay distinct from sensor judgments and notification records."""

from datetime import timedelta

import pytest
from test_monitoring_v2 import (
    NOW,
    assessment,
    login,
    monitoring_config,  # noqa: F401 -- share synthetic device/source setup
    send,
)

from src.app.models.log import CollectionLog
from src.app.services.collection_summary_v2_service import collection_summary
from src.app.services.monitoring_v2_service import monitoring_source

URL = "/api/v2/monitoring/collection-summary?pit_id=pit-demo"


def completion(when, pit_id="pit-demo"):
    return CollectionLog(
        pit_id=pit_id,
        completed_at=when,
        residual_height_cm=12,
        operator_name="Synthetic operator",
        notes="Synthetic completion",
    )


async def summary(db, days=30):
    return await collection_summary(db, monitoring_source("pit-demo"), days, NOW)


async def test_summary_authentication_scope_cache_and_window(client):
    assert (await client.get(URL)).status_code == 401
    await login(client)
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["source"] == "COLLECTION_LOG"
    assert data["lastCollectionAt"] is None
    assert data["secondsSinceLastCollection"] is None
    assert data["averageCollectionCycleSeconds"] is None
    assert data["completedCollectionsInWindow"] == data["cycleCount"] == 0
    assert data["reasonCodes"] == [
        "NO_CONFIRMED_COLLECTIONS",
        "INSUFFICIENT_COLLECTIONS_IN_WINDOW",
    ]
    assert data["computedAt"].endswith("Z")
    assert (await client.get(URL.replace("pit-demo", "unknown"))).status_code == 404
    for days in (0, 366, "bad"):
        assert (await client.get(URL + f"&window_days={days}")).status_code == 422


async def test_elapsed_time_and_actual_interval_mean_ignore_other_pits_and_future(
    client, test_db_session
):
    db = test_db_session
    # Arrival order does not determine chronological completion order.
    db.add_all(
        [
            completion(NOW - timedelta(hours=1)),
            completion(NOW - timedelta(hours=10)),
            completion(NOW - timedelta(hours=7)),
            completion(NOW - timedelta(seconds=1), "other-pit"),
            completion(NOW + timedelta(hours=1)),
        ]
    )
    await db.commit()
    result = await summary(db)
    assert result.lastCollectionAt == NOW - timedelta(hours=1)
    assert result.secondsSinceLastCollection == 3600
    assert result.completedCollectionsInWindow == 3
    assert result.cycleCount == 2
    assert result.averageCollectionCycleSeconds == (3 * 3600 + 6 * 3600) / 2
    assert result.reasonCodes == []
    # The API must expose real completion statistics, not just the service helper.
    await login(client)
    data = (await client.get(URL)).json()
    assert data["completedCollectionsInWindow"] == 3
    assert data["averageCollectionCycleSeconds"] == 16200


async def test_old_last_completion_retained_but_not_used_as_window_interval(
    client, test_db_session
):
    db = test_db_session
    db.add(completion(NOW - timedelta(days=31)))
    await db.commit()
    result = await summary(db)
    assert result.lastCollectionAt == NOW - timedelta(days=31)
    assert result.secondsSinceLastCollection == 31 * 86400
    assert result.completedCollectionsInWindow == 0
    assert result.averageCollectionCycleSeconds is None
    assert result.reasonCodes == ["INSUFFICIENT_COLLECTIONS_IN_WINDOW"]
    db.add(completion(NOW - timedelta(days=1)))
    await db.commit()
    result = await summary(db)
    assert result.completedCollectionsInWindow == 1
    assert result.cycleCount == 0
    assert result.averageCollectionCycleSeconds is None
    wider = await summary(db, 60)
    assert wider.averageCollectionCycleSeconds == 30 * 86400


async def test_window_boundaries_and_real_zero_elapsed(client, test_db_session):
    db = test_db_session
    db.add_all(
        [
            completion(NOW - timedelta(days=30, microseconds=1)),
            completion(NOW - timedelta(days=30)),
            completion(NOW),
        ]
    )
    await db.commit()
    result = await summary(db)
    assert result.completedCollectionsInWindow == 2
    assert result.secondsSinceLastCollection == 0
    assert result.averageCollectionCycleSeconds == 30 * 86400


@pytest.mark.parametrize("stage", ["SUSPECTED", "CONFIRMED", "RESOLVED", "RETRACTED"])
async def test_edge_collection_event_is_not_a_confirmed_completion(client, test_db_session, stage):
    event = assessment("collection-event")
    event.update(
        assessment_type="EVENT",
        event={"kind": "COLLECTION", "stage": stage},
    )
    await send(client, event)
    result = await summary(test_db_session)
    assert result.lastCollectionAt is None
    assert result.completedCollectionsInWindow == 0
    assert result.averageCollectionCycleSeconds is None
