"""Committed administrator identity and setting values must remain an atomic history."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from test_admin_access import login

from src.app.models.auth import AuthUser
from src.app.models.config import PitConfig
from src.app.models.settings_history import SettingsHistory

HISTORY = "/api/v1/settings/history"
ALERTS = "/api/v1/settings/alerts"
PIT = "/api/v1/config/pits/pit-01"


async def current(client):
    response = await client.get(ALERTS)
    assert response.status_code == 200
    return {k: v for k, v in response.json().items() if k != "version"}, response.headers["etag"]


async def change_alert(client, csrf, **changes):
    payload, etag = await current(client)
    payload.update(changes)
    response = await client.put(
        ALERTS, json=payload, headers={"If-Match": etag, "X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text
    return payload


async def rows(db):
    return await db.scalar(select(func.count()).select_from(SettingsHistory))


async def test_history_requires_admin_without_initializing_settings(client, test_db_session):
    assert (await client.get(HISTORY)).status_code == 401
    await login(client, "operator")
    assert (await client.get(HISTORY)).status_code == 403
    await login(client)
    response = await client.get(HISTORY)
    assert response.status_code == 200 and response.json() == {"items": [], "nextCursor": None}
    assert response.headers["cache-control"] == "no-store"
    assert await rows(test_db_session) == 0
    assert await test_db_session.scalar(select(func.count()).select_from(PitConfig)) == 0


async def test_alert_before_after_actor_and_spoofed_actor_input(client, test_db_session):
    csrf = await login(client)
    before, _ = await current(client)
    expected = await change_alert(
        client, csrf, sendDelayMinutes=7, actorId="forged", csrfToken="secret"
    )
    out = (await client.get(HISTORY)).json()["items"][0]
    actor = await test_db_session.get(AuthUser, "admin")
    assert out["actor"] == {"id": "admin", "displayName": actor.display_name}
    assert out["pitId"] == "pit-01" and out["resource"] == "alert_settings"
    assert out["operation"] == "update" and out["before"] == before
    assert out["after"]["sendDelayMinutes"] == expected["sendDelayMinutes"] == 7
    assert out["changedFields"] == ["sendDelayMinutes"]
    assert "version" not in out["after"] and "actorId" not in out["after"]
    assert "secret" not in str(out) and "password" not in str(out)
    assert datetime.fromisoformat(
        out["occurredAt"].replace("Z", "+00:00")
    ).utcoffset() == timedelta(0)
    assert out["id"].isdigit()


async def test_two_administrators_and_name_snapshot_survive_later_name_change(
    client, test_db_session
):
    csrf = await login(client)
    await change_alert(client, csrf, sendDelayMinutes=4)
    operator = await test_db_session.get(AuthUser, "operator")
    test_db_session.add(
        AuthUser(
            username="admin-two",
            display_name="다른 관리자",
            role="administrator",
            password_hash=operator.password_hash,
        )
    )
    await test_db_session.commit()
    csrf = await login(client, "admin-two")
    await change_alert(client, csrf, sendDelayMinutes=8)
    await test_db_session.execute(
        update(AuthUser).where(AuthUser.username == "admin-two").values(display_name="바뀐 이름")
    )
    await test_db_session.commit()
    items = (await client.get(HISTORY)).json()["items"]
    assert [x["actor"]["id"] for x in items] == ["admin-two", "admin"]
    assert items[0]["actor"]["displayName"] == "다른 관리자"
    assert items[0]["before"] == items[1]["after"]


async def test_noop_stale_invalid_and_csrf_failures_leave_no_history(client, test_db_session):
    csrf = await login(client)
    payload, etag = await current(client)
    result = await client.put(
        ALERTS, json=payload, headers={"If-Match": etag, "X-CSRF-Token": csrf}
    )
    assert result.status_code == 200 and await rows(test_db_session) == 0
    assert (
        await client.put(ALERTS, json=payload, headers={"If-Match": etag, "X-CSRF-Token": csrf})
    ).status_code == 412
    headers = {"If-Match": result.headers["etag"], "X-CSRF-Token": csrf}
    assert (
        await client.put(ALERTS, json={**payload, "collectionThresholdPercent": 1}, headers=headers)
    ).status_code == 422
    assert (
        await client.put(ALERTS, json=payload, headers={"If-Match": headers["If-Match"]})
    ).status_code == 403
    assert await rows(test_db_session) == 0


async def test_pit_creation_update_and_noop(client, test_db_session):
    csrf = await login(client)
    headers = {"X-CSRF-Token": csrf}
    create = await client.put(
        PIT, json={"name": "시험 피트", "total_depth_cm": 320}, headers=headers
    )
    assert create.status_code == 200, create.text
    created = (await client.get(HISTORY)).json()["items"][0]
    assert created["before"] is None and created["operation"] == "create"
    assert created["after"]["total_depth_cm"] == 320
    assert created["changedFields"] == sorted(created["after"])
    result = await client.put(PIT, json={"name": "변경된 피트"}, headers=headers)
    assert result.status_code == 200
    updated = (await client.get(HISTORY)).json()["items"][0]
    assert updated["resource"] == "pit_config" and updated["before"] == created["after"]
    assert updated["changedFields"] == ["name"]
    assert "updated_at" not in updated["after"]
    assert (await client.put(PIT, json={}, headers=headers)).status_code == 200
    assert (await client.put(PIT, json={"name": "変"})).status_code == 403
    assert await rows(test_db_session) == 2


@pytest.mark.parametrize("path", [ALERTS, PIT, "/api/v1/config/pits/new-pit"])
async def test_history_insert_failure_rolls_back_entire_change(client, test_db_session, path):
    csrf = await login(client)
    baseline, etag = await current(client)
    pit_before = (await client.get(PIT)).json()

    def fail_history(session, context, instances):
        if any(isinstance(row, SettingsHistory) for row in session.new):
            raise OperationalError("synthetic", {"secret": "private-value"}, Exception("failed"))

    event.listen(test_db_session.sync_session, "before_flush", fail_history)
    try:
        response = await client.put(
            path,
            json={**baseline, "sendDelayMinutes": 9} if path == ALERTS else {"name": "failed-name"},
            headers={"If-Match": etag, "X-CSRF-Token": csrf},
        )
        assert response.status_code == 503 and "private-value" not in response.text
    finally:
        event.remove(test_db_session.sync_session, "before_flush", fail_history)
    assert (await current(client))[0] == baseline
    assert (await client.get(PIT)).json() == pit_before
    assert await rows(test_db_session) == 0
    assert await test_db_session.get(PitConfig, "new-pit") is None


async def test_filters_cursor_and_new_insert_do_not_repeat_prior_page(client):
    csrf = await login(client)
    await change_alert(client, csrf, sendDelayMinutes=1)
    await client.put(PIT, json={"name": "new name"}, headers={"X-CSRF-Token": csrf})
    await change_alert(client, csrf, sendDelayMinutes=2)
    first = (await client.get(HISTORY, params={"limit": 2})).json()
    assert len(first["items"]) == 2 and first["nextCursor"] == first["items"][-1]["id"]
    await change_alert(client, csrf, sendDelayMinutes=3)
    second = (await client.get(HISTORY, params={"limit": 2, "cursor": first["nextCursor"]})).json()
    assert len(second["items"]) == 1 and second["nextCursor"] is None
    assert not ({x["id"] for x in first["items"]} & {x["id"] for x in second["items"]})
    assert (
        len((await client.get(HISTORY, params={"resource": "alert_settings"})).json()["items"]) == 3
    )
    assert (
        len(
            (await client.get(HISTORY, params={"pit_id": "pit-01", "actor_id": "admin"})).json()[
                "items"
            ]
        )
        == 4
    )
    assert (await client.get(HISTORY, params={"pit_id": "other-pit"})).json()["items"] == []
    instant = first["items"][0]["occurredAt"]
    exact = (await client.get(HISTORY, params={"from": instant})).json()["items"]
    assert first["items"][0]["id"] in {x["id"] for x in exact}
    earlier = (await client.get(HISTORY, params={"to": instant})).json()["items"]
    assert first["items"][0]["id"] not in {x["id"] for x in earlier}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "0"},
        {"cursor": "-1"},
        {"cursor": "9223372036854775808"},
        {"cursor": "1e2"},
        {"resource": "password"},
        {"from": "2026-01-01T00:00:00"},
        {"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    ],
)
async def test_invalid_history_query_is_rejected(client, params):
    await login(client)
    assert (await client.get(HISTORY, params=params)).status_code == 422


async def test_pit_cas_failure_has_no_history_or_data_change(client, test_db_session, monkeypatch):
    csrf = await login(client)
    await current(client)
    before = (await client.get(PIT)).json()
    execute = test_db_session.execute

    async def conflict(statement, *args, **kwargs):
        if statement.is_update:
            statement = statement.where(PitConfig.id == "not-the-requested-pit")
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(test_db_session, "execute", conflict)
    response = await client.put(PIT, json={"name": "conflicted"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409
    monkeypatch.setattr(test_db_session, "execute", execute)
    assert (await client.get(PIT)).json() == before
    assert await rows(test_db_session) == 0


async def test_independent_sqlite_pit_writers_keep_one_valid_history_chain(tmp_path):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.app.api.v1.config import update_pit_config
    from src.app.db.base import Base
    from src.app.schemas.config import PitConfigUpdateRequest
    from src.app.schemas.session import Session, User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pit-writers.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(PitConfig(id="pit-01", name="before"))
            await db.commit()
        reads = 0
        ready = asyncio.Event()

        async def writer(name):
            nonlocal reads
            async with factory() as db:
                execute = db.execute

                async def synchronize(statement, *args, **kwargs):
                    nonlocal reads
                    result = await execute(statement, *args, **kwargs)
                    if statement.is_select:
                        reads += 1
                        if reads == 2:
                            ready.set()
                        await asyncio.wait_for(ready.wait(), 5)
                    return result

                db.execute = synchronize
                session = Session(
                    user=User(id=name, displayName=name, role="administrator"),
                    expiresAt=datetime.now(UTC) + timedelta(hours=1),
                    csrfToken="synthetic",
                )
                try:
                    return await update_pit_config(
                        "pit-01", PitConfigUpdateRequest(name=name), db, session
                    )
                except HTTPException as exc:
                    assert exc.status_code in (409, 503)
                    return None

        results = await asyncio.gather(writer("admin-a"), writer("admin-b"))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        async with factory() as db:
            entries = (await db.scalars(select(SettingsHistory))).all()
            assert len(entries) == 1 and entries[0].before["name"] == "before"
            assert entries[0].after["name"] == entries[0].actor_username == winners[0].name
            assert (await db.get(PitConfig, "pit-01")).name == winners[0].name
    finally:
        await engine.dispose()
