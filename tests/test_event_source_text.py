"""Result text is neutral while raw records and structured provenance survive reads."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_daily_recordings import login
from test_monitoring_v2 import NOW, monitoring_config  # noqa: F401

from src.app.core.config import get_settings
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob


async def seed(db, message):
    row = AlertLog(
        pit_id="pit-demo",
        triggered_at=NOW,
        fill_ratio_percent=92,
        channel="LOG",
        status="RECORDED",
        event_type="collection_required",
        severity="error",
        message=message,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.parametrize("legacy", [False, True])
async def test_history_notifications_csv_and_recordings_keep_result_and_original_record(
    client, test_db_session, legacy, monkeypatch
):
    monkeypatch.setattr(get_settings(), "MEDIA_DAILY_SERVICE_URL", "http://media.test/api/v2")
    monkeypatch.setattr(get_settings(), "MEDIA_PIT_ID", "pit-demo")
    result = "스크랩 적재율 92.0%: 수거 필요"
    original = "[시뮬레이션] " + result + " (라이다 v2 판정)" if legacy else result
    row = await seed(test_db_session, original)
    await login(client)
    params = {
        "pit_id": "pit-demo",
        "from": (NOW - timedelta(seconds=1)).isoformat(),
        "to": (NOW + timedelta(seconds=1)).isoformat(),
        "category": "alert",
    }
    events = await client.get("/api/v1/events", params=params)
    assert events.status_code == 200
    item = events.json()["items"][0]
    assert item["id"] == f"evt-alt-{row.id}" and item["detail"] == result
    history = await client.get("/api/v1/history/load", params=params)
    assert history.status_code == 200
    assert history.json()["eventMarkers"][0]["detail"] == result
    notices = await client.get("/api/v1/notifications?pit_id=pit-demo")
    assert notices.status_code == 200
    assert notices.json()["items"][0]["detail"] == result
    csv = await client.get("/api/v1/events.csv", params=params)
    assert csv.status_code == 200 and result in csv.text
    assert "시뮬레이션" not in csv.text and "라이다 v2 판정" not in csv.text
    recordings = await client.get("/api/v2/recordings/events", params=params)
    assert recordings.status_code == 200
    recorded = recordings.json()["items"][0]
    assert recorded["eventId"] == item["id"]
    assert "시뮬레이션" not in recorded["title"] and "라이다" not in recorded["title"]
    await test_db_session.refresh(row)
    assert row.message == original and row.fill_ratio_percent == 92
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0


@pytest.mark.parametrize("evidence,expected", [(True, True), (False, False), (None, True)])
async def test_structured_provenance_is_independent_of_display_text(
    client, test_db_session, evidence, expected
):
    row = await seed(test_db_session, "[시뮬레이션] 스크랩 적재율 92.0%: 수거 필요")
    test_db_session.add(
        EdgeV2AlertLink(
            alert_id=row.id,
            pit_id="pit-demo",
            site_id="site-demo",
            edge_id="edge-demo",
            assessment_id="synthetic-observation",
            message_id="synthetic-message",
            observed_at=NOW,
        )
    )
    if evidence is not None:
        test_db_session.add(
            EdgeV2Transition(
                site_id="site-demo",
                edge_id="edge-demo",
                kind="ALERT_CREATED",
                occurred_at=NOW,
                payload={
                    "pitId": "pit-demo",
                    "alertId": row.id,
                    "episodeId": "synthetic-episode",
                    "scopeToken": "synthetic-scope",
                    "observedAt": NOW.isoformat(),
                    "simulated": evidence,
                },
            )
        )
    await test_db_session.commit()
    await login(client)
    response = await client.get("/api/v2/monitoring/alerts?pit_id=pit-demo")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["simulated"] is expected
    assert response.json()["totalItems"] == 1
