"""All policy fields persist together with thresholds and enforce versioned writes."""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_admin_access import SETTINGS, login

from src.app.db.base import Base
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.schemas.admin import AlertSettingsUpdate
from src.app.services.admin_service import AdminService
from src.app.services.alert_policy_service import get_settings_snapshot, replace_settings

PATH = "/api/v1/settings/alerts"
POLICY = {
    **SETTINGS,
    "preCollectionAlert": {"enabled": False, "thresholdPercent": 70},
    "sendDelayMinutes": 5,
    "repeatIntervalMinutes": 45,
    "maximumRepeatCount": 7,
    "recoveryNotificationEnabled": False,
    "eventTypes": ["measurement_error", "device_error"],
}


async def test_all_settings_survive_get_and_independent_service(client, test_engine):
    csrf = await login(client)
    first = await client.get(PATH)
    result = await client.put(
        PATH, json=POLICY, headers={"X-CSRF-Token": csrf, "If-Match": first.headers["etag"]}
    )
    assert result.status_code == 200
    assert {k: result.json()[k] for k in POLICY} == POLICY
    read = await client.get(PATH)
    assert read.json() == result.json()
    assert read.headers["etag"] == result.headers["etag"]
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        settings, etag = await AdminService().get_alert_settings("pit-01", db)
    assert settings.model_dump() == result.json()
    assert etag == result.headers["etag"]


async def test_existing_pit_retains_thresholds_when_default_policy_is_initialized(test_db_session):
    db = test_db_session
    db.add(
        PitConfig(
            id="pit-existing",
            name="Synthetic existing",
            total_depth_cm=400,
            warning_threshold_percent=72,
            critical_threshold_percent=93,
        )
    )
    await db.commit()
    result, _ = await get_settings_snapshot(db, "pit-existing")
    assert result.collectionThresholdPercent == 93
    assert result.preCollectionAlert.thresholdPercent == 72
    assert result.repeatIntervalMinutes == 30
    assert (await db.get(PitConfig, "pit-existing")).total_depth_cm == 400
    assert await db.scalar(select(func.count()).select_from(AlertPolicy)) == 1
    await get_settings_snapshot(db, "pit-existing")
    assert await db.scalar(select(func.count()).select_from(AlertPolicy)) == 1


async def test_policies_are_isolated_between_pits(test_db_session):
    db = test_db_session
    _, etag = await get_settings_snapshot(db, "pit-a")
    await replace_settings(db, "pit-a", AlertSettingsUpdate.model_validate(POLICY), etag)
    other, _ = await get_settings_snapshot(db, "pit-b")
    assert other.preCollectionAlert.enabled is True
    assert other.sendDelayMinutes == 0
    assert other.repeatIntervalMinutes == 30
    assert len(other.eventTypes) == 3


async def test_policy_only_change_updates_etag_and_stale_write_does_not_overwrite(client):
    csrf = await login(client)
    first = await client.get(PATH)
    payload = {k: first.json()[k] for k in POLICY}
    payload["sendDelayMinutes"] = 9
    result = await client.put(
        PATH, json=payload, headers={"X-CSRF-Token": csrf, "If-Match": first.headers["etag"]}
    )
    assert result.status_code == 200
    assert result.headers["etag"] != first.headers["etag"]
    stale = await client.put(
        PATH, json=POLICY, headers={"X-CSRF-Token": csrf, "If-Match": first.headers["etag"]}
    )
    assert stale.status_code == 412
    assert (await client.get(PATH)).json() == result.json()


async def test_missing_if_match_does_not_initialize_settings(client, test_db_session):
    csrf = await login(client)
    result = await client.put(PATH, json=POLICY, headers={"X-CSRF-Token": csrf})
    assert result.status_code == 428
    for model in (PitConfig, AlertPolicy):
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_pit_api_change_invalidates_policy_etag(client):
    csrf = await login(client)
    first = await client.get(PATH)
    changed = await client.put(
        "/api/v1/config/pits/pit-01",
        json={"critical_threshold_percent": 94},
        headers={"X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200
    stale = await client.put(
        PATH, json=POLICY, headers={"X-CSRF-Token": csrf, "If-Match": first.headers["etag"]}
    )
    assert stale.status_code == 412
    assert (await client.get(PATH)).json()["collectionThresholdPercent"] == 94


async def test_commit_failure_rolls_back_thresholds_and_policy(
    client, test_db_session, monkeypatch
):
    csrf = await login(client)
    first = await client.get(PATH)
    db = test_db_session
    original = db.commit

    async def broken():
        raise OperationalError("synthetic SQL", {"secret": "must-not-leak"}, Exception("failed"))

    monkeypatch.setattr(db, "commit", broken)
    result = await client.put(
        PATH, json=POLICY, headers={"X-CSRF-Token": csrf, "If-Match": first.headers["etag"]}
    )
    assert result.status_code == 503 and "must-not-leak" not in result.text
    monkeypatch.setattr(db, "commit", original)
    assert (await client.get(PATH)).json() == first.json()


async def test_policy_and_thresholds_survive_file_database_reopen(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            _, etag = await get_settings_snapshot(db, "pit-01")
            expected, saved_etag = await replace_settings(
                db, "pit-01", AlertSettingsUpdate.model_validate(POLICY), etag
            )
    finally:
        await engine.dispose()
    reopened = create_async_engine(url)
    try:
        factory = async_sessionmaker(reopened, expire_on_commit=False)
        async with factory() as db:
            actual, actual_etag = await get_settings_snapshot(db, "pit-01")
            assert actual == expected and actual_etag == saved_etag
    finally:
        await reopened.dispose()


async def test_concurrent_writes_cannot_mix_thresholds_and_policy(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            _, etag = await get_settings_snapshot(db, "pit-01")
        payloads = [POLICY, {**POLICY, "collectionThresholdPercent": 96, "sendDelayMinutes": 12}]

        async def writer(payload):
            async with factory() as db:
                try:
                    result, _ = await replace_settings(
                        db, "pit-01", AlertSettingsUpdate.model_validate(payload), etag
                    )
                    return result
                except HTTPException as error:
                    assert error.status_code in (412, 503)
                    return None

        results = await asyncio.gather(*(writer(p) for p in payloads))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        async with factory() as db:
            actual, _ = await get_settings_snapshot(db, "pit-01")
        assert actual == winners[0]
    finally:
        await engine.dispose()


async def test_policy_cas_failure_rolls_back_pit_update(test_db_session, monkeypatch):
    db = test_db_session
    baseline, etag = await get_settings_snapshot(db, "pit-01")
    real_execute = db.execute
    calls = 0

    async def conflict(statement, *args, **kwargs):
        nonlocal calls
        if statement.is_update:
            calls += 1
            if calls == 1:
                statement = statement.where(AlertPolicy.version_id == -1)
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", conflict)
    with pytest.raises(HTTPException) as error:
        await replace_settings(db, "pit-01", AlertSettingsUpdate.model_validate(POLICY), etag)
    assert error.value.status_code == 412
    monkeypatch.setattr(db, "execute", real_execute)
    assert (await get_settings_snapshot(db, "pit-01"))[0] == baseline


async def test_default_initialization_commit_failure_leaves_no_partial_rows(
    test_db_session, monkeypatch
):
    db = test_db_session

    async def broken():
        raise OperationalError("synthetic SQL", {}, Exception("failed"))

    monkeypatch.setattr(db, "commit", broken)
    with pytest.raises(HTTPException) as error:
        await get_settings_snapshot(db, "pit-new")
    assert error.value.status_code == 503
    for model in (PitConfig, AlertPolicy):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_unknown_pit_put_does_not_create_defaults(test_db_session):
    db = test_db_session
    with pytest.raises(HTTPException) as error:
        await replace_settings(
            db, "pit-new", AlertSettingsUpdate.model_validate(POLICY), '"unknown"'
        )
    assert error.value.status_code == 412
    for model in (PitConfig, AlertPolicy):
        assert await db.scalar(select(func.count()).select_from(model)) == 0
