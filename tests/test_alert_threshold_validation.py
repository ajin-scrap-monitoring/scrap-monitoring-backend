"""Saved thresholds remain evaluable, including partial and concurrent edits."""

import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_admin_access import login
from test_edge_v2_operations import operations_config, tick  # noqa: F401
from test_monitoring_v2 import NOW, monitoring_config  # noqa: F401

from src.app.api.v1.config import update_pit_config
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog
from src.app.schemas.config import PitConfigUpdateRequest
from src.app.services.active_alerts_v2_service import active_fill_alerts
from src.app.services.monitoring_v2_service import monitoring_source

PATH = "/api/v1/settings/alerts?pit_id=pit-demo"


@pytest.mark.parametrize(
    "critical,warning", [(90, 89), (90, 90), (90, 95), (4, 0), (0, 0), (90, 85.000001)]
)
async def test_unusable_thresholds_do_not_replace_settings(client, critical, warning):
    csrf = await login(client)
    original = await client.get(PATH)
    for enabled in (True, False):
        payload = {k: v for k, v in original.json().items() if k != "version"}
        payload.update(
            collectionThresholdPercent=critical,
            preCollectionAlert={"enabled": enabled, "thresholdPercent": warning},
            sendDelayMinutes=12,
        )
        result = await client.put(
            PATH,
            json=payload,
            headers={"X-CSRF-Token": csrf, "If-Match": original.headers["etag"]},
        )
        assert result.status_code == 422
        current = await client.get(PATH)
        assert current.json() == original.json()
        assert current.headers["etag"] == original.headers["etag"]


@pytest.mark.parametrize("critical,warning", [(5, 0), (90, 85), (100, 95)])
async def test_exact_recovery_boundary_is_a_valid_saved_warning(client, critical, warning):
    csrf = await login(client)
    original = await client.get(PATH)
    payload = {k: v for k, v in original.json().items() if k != "version"}
    payload.update(
        collectionThresholdPercent=critical,
        preCollectionAlert={"enabled": True, "thresholdPercent": warning},
    )
    result = await client.put(
        PATH,
        json=payload,
        headers={"X-CSRF-Token": csrf, "If-Match": original.headers["etag"]},
    )
    assert result.status_code == 200
    assert result.json()["collectionThresholdPercent"] == critical
    assert result.json()["preCollectionAlert"]["thresholdPercent"] == warning


async def test_invalid_first_save_creates_no_pit_or_policy(client, test_db_session):
    csrf = await login(client)
    result = await client.put(
        PATH,
        json={
            "collectionThresholdPercent": 90,
            "preCollectionAlert": {"enabled": True, "thresholdPercent": 89},
            "eventTypes": ["collection_required"],
        },
        headers={"X-CSRF-Token": csrf, "If-Match": '"not-created"'},
    )
    assert result.status_code == 422
    for model in (PitConfig, AlertPolicy):
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_partial_pit_changes_validate_against_saved_or_default_values(client):
    csrf = await login(client)
    headers = {"X-CSRF-Token": csrf}
    settings = await client.get(PATH)
    path = "/api/v1/config/pits/pit-demo"
    before = (await client.get(path)).json()
    for changes in [
        {"warning_threshold_percent": 81},
        {"critical_threshold_percent": 79},
        {"warning_threshold_percent": 86, "critical_threshold_percent": 90},
    ]:
        result = await client.put(
            path,
            json={**changes, "name": "Must not save", "total_depth_cm": 500},
            headers=headers,
        )
        assert result.status_code == 422
        assert (await client.get(path)).json() == before
        unchanged = await client.get(PATH)
        assert unchanged.json() == settings.json()
        assert unchanged.headers["etag"] == settings.headers["etag"]
    for index, changes in enumerate(
        [{"warning_threshold_percent": 84}, {"critical_threshold_percent": 70}]
    ):
        new_path = f"/api/v1/config/pits/synthetic-invalid-{index}"
        assert (await client.put(new_path, json=changes, headers=headers)).status_code == 422
        assert (await client.get(new_path)).status_code == 404
    valid = await client.put(path, json={"warning_threshold_percent": 80}, headers=headers)
    assert valid.status_code == 200 and valid.json()["warning_threshold_percent"] == 80


async def test_saved_threshold_and_pre_alert_switch_apply_to_new_observations(
    client, test_db_session
):
    csrf = await login(client)
    response = await client.get(PATH)
    payload = {k: v for k, v in response.json().items() if k != "version"}

    async def save(critical, warning, enabled):
        nonlocal response
        payload.update(
            collectionThresholdPercent=critical,
            preCollectionAlert={"enabled": enabled, "thresholdPercent": warning},
        )
        response = await client.put(
            PATH,
            json=payload,
            headers={"X-CSRF-Token": csrf, "If-Match": response.headers["etag"]},
        )
        assert response.status_code == 200, response.text

    async def active(seconds):
        return await active_fill_alerts(
            test_db_session, monitoring_source("pit-demo"), NOW + timedelta(seconds=seconds)
        )

    await save(95, 70, False)
    first = await tick(client, test_db_session, 0.94, 0)
    assert first.operations.effectiveState == "WARNING"
    assert first.summary.collectionThresholdPercent == 95
    assert (await active(0)).items == []
    await save(95, 70, True)
    await tick(client, test_db_session, 0.94, 1)
    assert (await active(1)).items[0].type == "pre_collection_alert"
    await save(90, 75, True)
    current = await tick(client, test_db_session, 0.94, 2)
    assert current.operations.effectiveState == "CRITICAL"
    assert current.summary.recoveryThresholdPercent == 85
    assert (await active(2)).items[0].type == "collection_required"
    await save(90, 75, False)
    assert (await active(2)).items[0].type == "collection_required"
    assert (await tick(client, test_db_session, 0.86, 3)).operations.effectiveState == "CRITICAL"
    assert (await tick(client, test_db_session, 0.85, 4)).operations.effectiveState == "WARNING"
    assert (await tick(client, test_db_session, 0.95, 5)).operations.effectiveState == "CRITICAL"
    assert await test_db_session.scalar(select(func.count()).select_from(AlertLog)) == 1


async def test_postgresql_partial_edit_validates_the_pair_after_another_writer_commits(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Concurrent row locks require the isolated PostgreSQL fixture")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as setup:
        setup.add(
            PitConfig(
                id="pit-demo",
                name="Synthetic",
                warning_threshold_percent=75,
                critical_threshold_percent=90,
            )
        )
        await setup.commit()
    async with factory() as first, factory() as second:
        pit = await first.scalar(
            select(PitConfig).where(PitConfig.id == "pit-demo").with_for_update()
        )
        pit.critical_threshold_percent = 85
        await first.flush()
        task = asyncio.create_task(
            update_pit_config(
                "pit-demo", PitConfigUpdateRequest(warning_threshold_percent=85), second
            )
        )
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            await first.commit()
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(task, timeout=5)
            assert error.value.status_code == 422
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await first.rollback()
            await second.rollback()
    async with factory() as check:
        saved = await check.get(PitConfig, "pit-demo")
        assert (saved.warning_threshold_percent, saved.critical_threshold_percent) == (75, 85)
