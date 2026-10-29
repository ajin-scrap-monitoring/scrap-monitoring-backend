"""Reject invalid manual collection inputs before persistence or state changes."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from test_media_control import fake_media as fake_media
from test_media_control import login

from src.app.models.log import CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.services.broadcaster import broadcaster
from src.app.services.state_engine import state_engine

API = "/api/v1/collections/confirm"
BODY = {"pit_id": "pit-01", "residual_height_cm": 15, "operator_name": "Synthetic operator"}


@pytest.mark.parametrize(
    "field,value,raw",
    [
        ("pit_id", "x" * 51, False),
        ("pit_id", "가" * 51, False),
        ("residual_height_cm", "Infinity", False),
        ("residual_height_cm", "-Infinity", False),
        ("residual_height_cm", "NaN", False),
        ("residual_height_cm", "1e400", False),
        ("residual_height_cm", -0.1, False),
        ("residual_height_cm", "1e400", True),
        ("residual_height_cm", "NaN", True),
        ("residual_height_cm", "Infinity", True),
        ("residual_height_cm", "-Infinity", True),
    ],
)
async def test_invalid_manual_collection_preserves_records_jobs_and_state(
    client, test_db_session, fake_media, monkeypatch, field, value, raw
):
    headers = await login(client)
    previous = CollectionLog(
        pit_id="pit-01",
        completed_at=datetime.now(UTC),
        residual_height_cm=10,
        operator_name="Previous synthetic operator",
        notes="Synthetic preserved note",
    )
    test_db_session.add(previous)
    await test_db_session.commit()
    await test_db_session.refresh(previous)
    previous_values = (previous.completed_at, previous.residual_height_cm, previous.notes)

    def forbidden_reset(*args, **kwargs):
        pytest.fail("Invalid collection input must not reset the current pit state")

    async def forbidden_broadcast(*args, **kwargs):
        pytest.fail("Invalid collection input must not broadcast a completed collection")

    monkeypatch.setattr(state_engine, "reset_pit_state", forbidden_reset)
    monkeypatch.setattr(broadcaster, "broadcast", forbidden_broadcast)
    if raw:
        content = (
            '{"pit_id":"pit-01","operator_name":"Synthetic operator","residual_height_cm":'
            + value
            + "}"
        )
        response = await client.post(
            API, content=content, headers={**headers, "Content-Type": "application/json"}
        )
    else:
        response = await client.post(API, json={**BODY, field: value}, headers=headers)
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", field] for error in response.json()["detail"])
    json.dumps(response.json(), allow_nan=False)
    assert await test_db_session.scalar(select(func.count()).select_from(CollectionLog)) == 1
    assert await test_db_session.scalar(select(func.count()).select_from(MediaClipJob)) == 0
    await test_db_session.refresh(previous)
    assert (previous.completed_at, previous.residual_height_cm, previous.notes) == previous_values
    assert fake_media.requests == []


@pytest.mark.parametrize("pit_id", ["pit-01", "x" * 50, "가" * 50])
@pytest.mark.parametrize("height", [0, 15.5])
async def test_valid_manual_collection_preserves_confirmation_and_queue_contract(
    client, test_db_session, fake_media, monkeypatch, pit_id, height
):
    headers = await login(client)
    resets, broadcasts = [], []
    monkeypatch.setattr(state_engine, "reset_pit_state", lambda *a, **k: resets.append((a, k)))

    async def broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(broadcaster, "broadcast", broadcast)
    response = await client.post(
        API, json={**BODY, "pit_id": pit_id, "residual_height_cm": height}, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["pit_id"] == pit_id and body["residual_height_cm"] == height
    assert len(resets) == len(broadcasts) == 1
    assert broadcasts[0].calculated_height_cm == height
    assert await test_db_session.scalar(select(func.count()).select_from(CollectionLog)) == 1
    jobs = (await test_db_session.scalars(select(MediaClipJob))).all()
    assert len(jobs) == (1 if pit_id == "pit-01" else 0)
    if jobs:
        assert jobs[0].event_id == f"evt-col-{body['id']}" and jobs[0].category == "collection"
    assert fake_media.requests == []
