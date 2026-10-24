"""Stored v2 changes must appear in ordinary history without declaring current health."""

from datetime import timedelta

from sqlalchemy import func, select
from test_monitoring_v2 import NOW, monitoring_config  # noqa: F401

from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob

URL = "/api/v1/events?pit_id=pit-demo&category=error"


def transition(seconds, kind="DEVICE_STATE_CHANGED", site="site-demo", edge="edge-demo", **payload):
    return EdgeV2Transition(
        site_id=site,
        edge_id=edge,
        occurred_at=NOW + timedelta(seconds=seconds),
        kind=kind,
        payload={
            "previous": "DEGRADED",
            "current": "MEASUREMENT_UNAVAILABLE",
            "reasonCodes": ["SCAN_NOT_UPDATED"],
            **payload,
        },
    )


async def test_stored_device_failure_and_preparation_are_visible(client, test_db_session):
    db = test_db_session
    db.add_all(
        [
            transition(-20),
            transition(
                -10,
                previous="MEASUREMENT_UNAVAILABLE",
                current="DEGRADED",
                reasonCodes=["SCAN_RECOVERING"],
            ),
        ]
    )
    await db.commit()
    response = await client.get(URL)
    assert response.status_code == 200
    data = response.json()
    assert data["page"]["totalItems"] == 2
    assert [e["stateAtOccurrence"] for e in data["items"]] == ["PREPARING", "UNAVAILABLE"]
    assert all(e["statusBasis"] == "AT_OCCURRENCE" for e in data["items"])
    assert all(e["eventSource"] == "EDGE_V2_TRANSITION" for e in data["items"])
    assert all(e["pitAttribution"] == "CURRENT_ASSIGNMENT" for e in data["items"])
    assert data["items"][0]["severity"] == "info"
    assert data["items"][1]["reasonCodes"] == ["SCAN_NOT_UPDATED"]


async def test_measurement_recovery_is_distinct_and_query_does_not_write(client, test_db_session):
    db = test_db_session
    db.add_all(
        [
            transition(
                -20,
                kind="MEASUREMENT_VALIDITY_CHANGED",
                previousValid=True,
                currentValid=False,
                reasonCodes=["HEIGHT_SCAN_UNAVAILABLE"],
                pitId="pit-demo",
                simulated=True,
            ),
            transition(
                -10,
                kind="MEASUREMENT_VALIDITY_CHANGED",
                previousValid=False,
                currentValid=True,
                reasonCodes=[],
                pitId="pit-demo",
                simulated=True,
            ),
        ]
    )
    await db.commit()
    data = (await client.get(URL)).json()
    assert data["page"]["totalItems"] == 2
    assert data["items"][0]["stateAtOccurrence"] == "RECOVERED"
    assert data["items"][0]["title"] == "측정 입력 복구"
    assert data["items"][0]["pitAttribution"] == "RECORDED"
    assert data["items"][0]["simulated"] is True
    assert data["items"][1]["resolvedAt"] is None
    assert data["items"][0]["id"] != data["items"][1]["id"]
    assert (await client.get(URL)).json() == data
    assert await db.scalar(select(func.count()).select_from(EdgeV2Transition)) == 2
    for model in (AlertLog, MediaClipJob):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_history_scope_filters_pit_site_device_kind_and_future(client, test_db_session):
    db = test_db_session
    db.add_all(
        [
            transition(-10, pitId="pit-demo"),
            transition(-11, pitId="other-pit"),
            transition(-12, site="other-site"),
            transition(-13, edge="other-edge"),
            transition(-14, kind="FILL_STATE_CHANGED"),
            transition(365 * 86400),
        ]
    )
    await db.commit()
    data = (await client.get(URL)).json()
    assert data["page"]["totalItems"] == 1
    assert data["items"][0]["pitAttribution"] == "RECORDED"
    assert (await client.get(URL.replace("pit-demo", "unconfigured"))).json()["items"] == []
    assert (await client.get(URL.replace("category=error", "category=collection"))).json()[
        "items"
    ] == []
    assert (await client.get(URL.replace("category=error", "category=alert"))).json()["items"] == []
    assert (await client.get(URL.replace("category=error", "category=all"))).json()["page"][
        "totalItems"
    ] == 1


async def test_error_history_time_offsets_boundaries_and_page_order(client, test_db_session):
    from datetime import timezone

    db = test_db_session
    rows = [transition(second) for second in [-121, -120, -90, -60, -59]]
    db.add_all(rows)
    await db.commit()
    plus_nine = timezone(timedelta(hours=9))
    params = {
        "pit_id": "pit-demo",
        "category": "error",
        "from": (NOW - timedelta(seconds=120)).astimezone(plus_nine).isoformat(),
        "to": (NOW - timedelta(seconds=60)).isoformat(),
        "pageSize": 1,
    }
    pages = []
    for page in [1, 2, 3, 4]:
        data = (await client.get(URL, params={**params, "page": page})).json()
        assert data["page"]["totalItems"] == data["page"]["totalPages"] == 3
        pages.append(data["items"])
    assert [p[0]["transitionId"] for p in pages[:3]] == [rows[3].id, rows[2].id, rows[1].id]
    assert pages[3] == []


async def test_status_receiver_records_pit_and_replays_do_not_duplicate_history(
    client, test_db_session, monkeypatch
):
    from test_monitoring_v2 import send, status

    from src.app.core.config import get_settings

    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    healthy = status("healthy-before", NOW - timedelta(seconds=3), 1)
    failed = status("failed", NOW - timedelta(seconds=2), 2)
    failed.update(overall_state="MEASUREMENT_UNAVAILABLE", reason_codes=["SCAN_NOT_UPDATED"])
    recovered = status("healthy-after", NOW - timedelta(seconds=1), 3)
    await send(client, healthy)
    await send(client, failed)
    repeated = await send(client, failed)
    assert repeated.json()["duplicate"]
    await send(client, recovered)
    data = (await client.get(URL)).json()
    assert data["page"]["totalItems"] == 2
    assert [e["stateAtOccurrence"] for e in data["items"]] == ["RECOVERED", "UNAVAILABLE"]
    assert all(e["pitAttribution"] == "RECORDED" for e in data["items"])
    assert data["items"][0]["resolvedAt"] == data["items"][0]["occurredAt"]
    assert data["items"][1]["resolvedAt"] is None


async def test_preparing_and_unknown_records_are_not_declared_sensor_faults(
    client, test_db_session
):
    db = test_db_session
    db.add_all(
        [
            transition(-30, current="UNKNOWN", reasonCodes=[]),
            transition(
                -20,
                kind="MEASUREMENT_VALIDITY_CHANGED",
                previousValid=True,
                currentValid=False,
                reasonCodes=["FILTER_WARMING"],
            ),
            transition(-10, current="BROKEN_PAYLOAD", reasonCodes=None),
            transition(
                -5, kind="MEASUREMENT_VALIDITY_CHANGED", previousValid=True, currentValid=True
            ),
        ]
    )
    await db.commit()
    data = (await client.get(URL)).json()
    assert data["page"]["totalItems"] == 2
    assert [e["stateAtOccurrence"] for e in data["items"]] == ["PREPARING", "UNKNOWN"]
    assert all(e["severity"] != "error" for e in data["items"])
    assert data["items"][0]["resolvedAt"] is None


async def test_measurement_receiver_preserves_recorded_pit_and_input_origin(
    client, test_db_session, monkeypatch
):
    from test_monitoring_v2 import assessment, send, status

    from src.app.core.config import get_settings

    monkeypatch.setenv("EDGE_V2_COMPACT_STORAGE_ENABLED", "true")
    get_settings.cache_clear()
    await send(client, status("status-for-input", NOW - timedelta(seconds=10), 1))
    for second in range(5):
        await send(client, assessment(f"valid-{second}", 0.7, NOW - timedelta(seconds=6 - second)))
    failed = assessment("input-failed", 0.7, NOW - timedelta(seconds=1))
    failed.update(
        decision_state="UNAVAILABLE", fill_estimate=None, reason_codes=["SENSOR_UNAVAILABLE"]
    )
    await send(client, failed)
    repeated = await send(client, failed)
    assert repeated.json()["duplicate"]
    data = (await client.get(URL)).json()
    items = [e for e in data["items"] if e["transitionKind"] == "MEASUREMENT_VALIDITY_CHANGED"]
    assert len(items) == 1
    assert items[0]["stateAtOccurrence"] == "UNAVAILABLE"
    assert items[0]["pitAttribution"] == "RECORDED"
    assert items[0]["simulated"] is False
    assert items[0]["reasonCodes"] == ["SENSOR_UNAVAILABLE"]
