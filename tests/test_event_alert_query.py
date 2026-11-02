"""Grouped alert origins preserve the old first-ID and source isolation rules."""

from datetime import timedelta

import pytest
from sqlalchemy import String, and_, cast, func, select, text
from sqlalchemy.orm import aliased
from test_daily_recordings import login
from test_event_recordings import API, BASE, NOW
from test_event_recordings import archive as archive

from src.app.models.config import PitConfig
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.event_recording_queries import event_table


@pytest.fixture
async def linked_alerts(test_db_session):
    db = test_db_session
    db.add_all(
        [PitConfig(id="pit-demo", name="Synthetic"), PitConfig(id="pit-other", name="Other")]
    )
    await db.flush()
    alerts = [
        AlertLog(
            pit_id="pit-demo",
            triggered_at=BASE,
            fill_ratio_percent=95,
            channel="LOG",
            status="RECORDED",
            message=f"Synthetic {n}",
        )
        for n in range(12)
    ]
    db.add_all(alerts)
    await db.flush()
    first = None
    for n, alert in enumerate(alerts):
        if n != 2:
            db.add(
                EdgeV2AlertLink(
                    alert_id=alert.id,
                    pit_id="pit-other" if n == 3 else "pit-demo",
                    site_id="other-site" if n == 4 else "site-demo",
                    edge_id="other-edge" if n == 5 else "edge-demo",
                    assessment_id=f"assessment-{n}",
                    message_id=f"message-{n}",
                    observed_at=BASE,
                )
            )
        payload = {
            "pitId": "pit-other" if n == 10 else "pit-demo",
            "alertId": alert.id if n == 1 else str(alert.id),
            "simulated": True,
        }
        if n == 8:
            payload["alertId"] = "0" + str(alert.id)
        if n == 9:
            del payload["alertId"]
        row = EdgeV2Transition(
            site_id="site-demo",
            edge_id="edge-demo",
            kind="OTHER_KIND" if n == 6 else "ALERT_CREATED",
            occurred_at=BASE + timedelta(minutes=10),
            payload=payload,
        )
        db.add(row)
        await db.flush()
        if n == 0:
            first = row
        if n in (0, 11):
            # A later ID has an earlier time; timestamp ordering must not replace first ID.
            db.add(
                EdgeV2Transition(
                    site_id="site-demo",
                    edge_id="edge-demo",
                    kind="ALERT_CREATED",
                    occurred_at=BASE - timedelta(hours=1),
                    payload={**payload, "simulated": False},
                )
            )
        if n == 7:
            db.add(
                EdgeV2Transition(
                    site_id="other-site",
                    edge_id="edge-demo",
                    kind="ALERT_CREATED",
                    occurred_at=BASE,
                    payload={**payload, "simulated": False},
                )
            )
    db.add(
        AlertLog(
            pit_id="pit-other",
            triggered_at=BASE,
            fill_ratio_percent=95,
            channel="LOG",
            status="RECORDED",
            message="Other pit",
        )
    )
    await db.commit()
    return db, alerts, first


def old_origin_query(source):
    """Independent legacy selector for equivalence, including malformed JSON keys."""
    created = aliased(EdgeV2Transition)
    payload = (
        select(created.payload)
        .join(
            EdgeV2AlertLink,
            and_(
                created.site_id == EdgeV2AlertLink.site_id,
                created.edge_id == EdgeV2AlertLink.edge_id,
                cast(created.payload["alertId"].as_string(), String)
                == cast(EdgeV2AlertLink.alert_id, String),
            ),
        )
        .where(
            EdgeV2AlertLink.alert_id == AlertLog.id,
            EdgeV2AlertLink.pit_id == "pit-demo",
            EdgeV2AlertLink.site_id == source.site_id,
            EdgeV2AlertLink.edge_id == source.edge_id,
            created.kind == "ALERT_CREATED",
            created.payload["pitId"].as_string() == "pit-demo",
        )
        .correlate(AlertLog)
        .order_by(created.id)
        .limit(1)
        .scalar_subquery()
    )
    return select(AlertLog.id, payload.label("payload")).where(AlertLog.pit_id == "pit-demo")


@pytest.mark.parametrize("category", ["alert", "all"])
@pytest.mark.parametrize("restricted", ["all", "one", "ids", "no_source"])
async def test_grouped_origins_match_legacy_payloads_without_duplicate_alerts(
    linked_alerts, category, restricted
):
    db, alerts, first = linked_alerts
    source = MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")
    expected = {r.id: r.payload for r in (await db.execute(old_origin_query(source)))}
    kwargs = {}
    if restricted == "one":
        kwargs["only_id"] = f"evt-alt-{alerts[0].id}"
        expected = {alerts[0].id: expected[alerts[0].id]}
    elif restricted == "ids":
        kwargs["only_ids"] = {f"evt-alt-{alerts[n].id}" for n in (0, 2, 7)}
        expected = {alerts[n].id: expected[alerts[n].id] for n in (0, 2, 7)}
    elif restricted == "no_source":
        source = None
        expected = dict.fromkeys(expected)
    table = event_table(
        db, "pit-demo", source, category, BASE - timedelta(days=1), NOW, NOW, 30, 30, None, **kwargs
    )
    rows = (await db.execute(select(table))).mappings().all()
    assert len(rows) == len(expected)
    assert {r["number"]: r["payload"] for r in rows} == expected
    assert await db.scalar(select(func.count()).select_from(MediaClipJob)) == 0


async def test_stable_page_freezes_grouped_origin_metadata(client, linked_alerts, archive):
    db, alerts, first = linked_alerts
    identifier = f"evt-alt-{alerts[0].id}"
    first_id = first.id
    await login(client)
    params = {
        "stable": True,
        "category": "alert",
        "pageSize": 100,
        "from": (BASE - timedelta(days=1)).isoformat(),
        "to": NOW.isoformat(),
    }
    response = await client.get(API, params=params)
    assert response.status_code == 200
    original = response.json()
    assert next(i for i in original["items"] if i["eventId"] == identifier)["simulated"] is True
    # Authentication rolls back its read transaction and expires ORM instances.
    first = await db.get(EdgeV2Transition, first_id)
    first.payload = {**first.payload, "simulated": False}
    await db.commit()
    frozen = await client.get(API, params={**params, "snapshot_token": original["snapshotToken"]})
    assert frozen.status_code == 200 and frozen.json() == original
    refreshed = await client.get(API, params=params)
    assert refreshed.status_code == 200
    assert (
        next(i for i in refreshed.json()["items"] if i["eventId"] == identifier)["simulated"]
        is False
    )


async def test_postgresql_alert_plan_has_no_per_alert_correlated_subplan(linked_alerts, request):
    if not request.config.getoption("--postgresql"):
        pytest.skip("PostgreSQL plan regression")
    db, alerts, first = linked_alerts
    source = MonitoringSource(pit_id="pit-demo", site_id="site-demo", edge_id="edge-demo")
    table = event_table(
        db, "pit-demo", source, "alert", BASE - timedelta(days=1), NOW, NOW, 30, 30, None
    )
    statement = select(table)
    sql = str(
        statement.compile(dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True})
    )
    plan = await db.scalar(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql))
    if isinstance(plan, str):
        import json

        plan = json.loads(plan)

    def walk(node):
        assert node.get("Parent Relationship") != "SubPlan"
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
