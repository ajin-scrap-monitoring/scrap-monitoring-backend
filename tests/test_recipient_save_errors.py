"""Recipient input boundaries and duplicate saves across independent connections."""

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app.main import create_app
from src.app.models.recipient import NotificationRecipientModel
from src.app.schemas.admin import NotificationRecipientCreate, NotificationRecipientPatch
from src.app.services.admin_service import admin_service

API = "/api/v1/notification-recipients"
LIMITS = [("name", 100), ("team", 100), ("email", 255), ("phone", 50)]


def payload(**changes):
    return {
        "name": "Synthetic recipient",
        "team": "Test",
        "email": "recipient@example.com",
        "phone": "000-0000-0000",
        "channels": ["email"],
        "eventTypes": ["device_error"],
        **changes,
    }


async def login(client):
    response = await client.post(
        "/api/v1/session", json={"username": "admin", "password": "admin1234"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrfToken"]


def text_value(field, length):
    return (
        "e" * (length - len("@example.com")) + "@example.com" if field == "email" else "가" * length
    )


async def edit_target(client, method):
    if method == "POST":
        return API, payload()
    original = await client.post(API, json=payload())
    assert original.status_code == 201
    return API + "/" + original.json()["id"], {}


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("field,limit", LIMITS)
async def test_recipient_overlong_input_preserves_saved_data(client, method, field, limit):
    await login(client)
    path, body = await edit_target(client, method)
    before = (await client.get(API)).json()
    response = await client.request(
        method, path, json={**body, field: text_value(field, limit + 1)}
    )
    assert response.status_code == 422
    assert any(error["loc"][-1] == field for error in response.json()["detail"])
    assert (await client.get(API)).json() == before


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("field,limit", LIMITS)
async def test_recipient_accepts_database_field_limit(client, method, field, limit):
    await login(client)
    path, body = await edit_target(client, method)
    value = text_value(field, limit)
    response = await client.request(method, path, json={**body, field: value})
    assert response.status_code == (201 if method == "POST" else 200)
    assert response.json()[field] == value
    assert response.json()["version"] == response.headers["ETag"]
    assert (await client.get(API)).json()["items"][0][field] == value


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    "field,values", [("channels", ["email", "email"]), ("eventTypes", ["device_error"] * 2)]
)
async def test_recipient_duplicate_selections_preserve_saved_data(client, method, field, values):
    await login(client)
    path, body = await edit_target(client, method)
    before = (await client.get(API)).json()
    response = await client.request(method, path, json={**body, field: values})
    assert response.status_code == 422
    assert (await client.get(API)).json() == before


def test_recipient_openapi_declares_database_limits():
    schemas = create_app().openapi()["components"]["schemas"]
    for model in ["NotificationRecipientCreate", "NotificationRecipientPatch"]:
        properties = schemas[model]["properties"]
        for field, limit in LIMITS:
            field_schema = properties[field]
            choices = field_schema.get("anyOf", [field_schema])
            assert {"type": "string", "maxLength": limit}.items() <= next(
                choice for choice in choices if choice.get("type") == "string"
            ).items()
        for field, limit in [("channels", 2), ("eventTypes", 3)]:
            field_schema = properties[field]
            choices = field_schema.get("anyOf", [field_schema])
            assert (
                next(choice for choice in choices if choice.get("type") == "array")["maxItems"]
                == limit
            )


async def test_non_email_integrity_error_is_not_reported_as_duplicate(test_db_session, monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("src.app.services.admin_service").uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="deadbeef" * 4),
    )
    await admin_service.create_recipient(NotificationRecipientCreate(**payload()), test_db_session)
    with pytest.raises(IntegrityError):
        await admin_service.create_recipient(
            NotificationRecipientCreate(**payload(email="other@example.com")), test_db_session
        )
    assert (
        await test_db_session.scalar(select(func.count()).select_from(NotificationRecipientModel))
        == 1
    )


async def test_concurrent_duplicate_registration_returns_conflict(
    test_engine, test_db_session, monkeypatch, request
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Concurrent writes require independent PostgreSQL connections")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    ready, arrived = asyncio.Event(), 0
    async with factory() as first, factory() as second:
        for db in (first, second):
            commit = db.commit

            async def coordinated_commit(commit=commit):
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 10)
                await commit()

            monkeypatch.setattr(db, "commit", coordinated_commit)
        body = NotificationRecipientCreate(**payload(email="race@example.com"))
        results = await asyncio.wait_for(
            asyncio.gather(
                admin_service.create_recipient(body, first),
                admin_service.create_recipient(body, second),
                return_exceptions=True,
            ),
            20,
        )
        assert sum(isinstance(result, tuple) for result in results) == 1
        assert (
            sum(
                isinstance(result, HTTPException) and result.status_code == 409
                for result in results
            )
            == 1
        )
    assert (
        await test_db_session.scalar(select(func.count()).select_from(NotificationRecipientModel))
        == 1
    )


async def test_concurrent_email_updates_preserve_loser_and_version(
    test_engine, test_db_session, monkeypatch, request
):
    if not request.config.getoption("--postgresql"):
        pytest.skip("Concurrent writes require independent PostgreSQL connections")
    originals = []
    for email in ["first@example.com", "second@example.com"]:
        item, _ = await admin_service.create_recipient(
            NotificationRecipientCreate(**payload(email=email)), test_db_session
        )
        originals.append(item)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    ready, arrived = asyncio.Event(), 0
    async with factory() as first, factory() as second:
        for db in (first, second):
            execute = db.execute

            async def coordinated_execute(statement, *args, execute=execute, **kwargs):
                nonlocal arrived
                if getattr(statement, "is_update", False):
                    arrived += 1
                    if arrived == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 10)
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", coordinated_execute)
        body = NotificationRecipientPatch(email="shared@example.com")
        results = await asyncio.wait_for(
            asyncio.gather(
                admin_service.update_recipient(originals[0].id, body, first, '"1"'),
                admin_service.update_recipient(originals[1].id, body, second, '"1"'),
                return_exceptions=True,
            ),
            20,
        )
        assert sum(isinstance(result, tuple) for result in results) == 1
        assert (
            sum(
                isinstance(result, HTTPException) and result.status_code == 409
                for result in results
            )
            == 1
        )
    async with factory() as check:
        rows = (await check.scalars(select(NotificationRecipientModel))).all()
        assert len(rows) == 2
        assert sorted(row.version_id for row in rows) == [1, 2]
        assert sum(row.email == "shared@example.com" for row in rows) == 1
        loser = next(row for row in rows if row.version_id == 1)
        assert loser.email == next(item.email for item in originals if item.id == loser.id)
