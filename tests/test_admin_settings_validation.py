"""Invalid admin settings never become storage faults or persisted non-finite values."""

import json

import pytest
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select
from starlette.requests import Request
from test_admin_access import login

from src.app.api.errors import request_validation_error_response
from src.app.main import create_app
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.schemas.errors import RequestValidationErrorResponse

SETTINGS = "/api/v1/settings/alerts"
PITS = "/api/v1/config/pits"
FIELDS = [("sendDelayMinutes", 0), ("repeatIntervalMinutes", 0), ("maximumRepeatCount", 1)]


@pytest.mark.parametrize("field,minimum", FIELDS)
@pytest.mark.parametrize("outside", ["below", "above"])
async def test_policy_out_of_range_preserves_values_and_etag(client, field, minimum, outside):
    csrf = await login(client)
    original = await client.get(SETTINGS)
    body = {key: value for key, value in original.json().items() if key != "version"}
    body.update(
        collectionThresholdPercent=95, preCollectionAlert={"enabled": False, "thresholdPercent": 80}
    )
    body[field] = minimum - 1 if outside == "below" else 2147483648
    result = await client.put(
        SETTINGS, json=body, headers={"X-CSRF-Token": csrf, "If-Match": original.headers["ETag"]}
    )
    assert result.status_code == 422
    current = await client.get(SETTINGS)
    assert current.json() == original.json()
    assert current.headers["ETag"] == original.headers["ETag"]


@pytest.mark.parametrize("field,minimum", FIELDS)
@pytest.mark.parametrize("boundary", ["minimum", "maximum"])
async def test_policy_accepts_database_integer_boundaries(client, field, minimum, boundary):
    csrf = await login(client)
    original = await client.get(SETTINGS)
    body = {key: value for key, value in original.json().items() if key != "version"}
    body[field] = minimum if boundary == "minimum" else 2147483647
    result = await client.put(
        SETTINGS, json=body, headers={"X-CSRF-Token": csrf, "If-Match": original.headers["ETag"]}
    )
    assert result.status_code == 200
    assert result.json()[field] == body[field]
    assert (await client.get(SETTINGS)).json() == result.json()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "1e400"])
@pytest.mark.parametrize("raw", [False, True])
async def test_non_finite_depth_is_rejected_without_writes(
    client, test_db_session, existing, value, raw
):
    csrf = await login(client)
    if existing:
        original = await client.get(SETTINGS)
        path = PITS + "/pit-01"
    else:
        path = PITS + "/synthetic-new"
    data = (
        {
            "content": '{"total_depth_cm":' + value + "}",
            "headers": {"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        }
        if raw
        else {"json": {"total_depth_cm": value}, "headers": {"X-CSRF-Token": csrf}}
    )
    response = await client.put(path, **data)
    assert response.status_code == 422
    RequestValidationErrorResponse.model_validate(response.json())
    json.dumps(response.json(), allow_nan=False)
    if existing:
        assert (await client.get(SETTINGS)).json() == original.json()
        assert (await test_db_session.get(PitConfig, "pit-01")).total_depth_cm == 300
    else:
        assert await test_db_session.scalar(select(func.count()).select_from(PitConfig)) == 0
        assert await test_db_session.scalar(select(func.count()).select_from(AlertPolicy)) == 0


@pytest.mark.parametrize("existing", [False, True])
async def test_pit_name_limit_preserves_settings(client, test_db_session, existing):
    csrf = await login(client)
    if existing:
        original = await client.get(SETTINGS)
        path = PITS + "/pit-01"
    else:
        path = PITS + "/synthetic-new"
    response = await client.put(path, json={"name": "가" * 101}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 422
    if existing:
        assert (await client.get(SETTINGS)).json() == original.json()
    else:
        assert await test_db_session.scalar(select(func.count()).select_from(PitConfig)) == 0


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_pit_identifier_limit_does_not_create_data(client, test_db_session, method):
    csrf = await login(client)
    response = await client.request(
        method,
        PITS + "/" + "p" * 51,
        **({"json": {"name": "Synthetic"}} if method == "PUT" else {}),
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert await test_db_session.scalar(select(func.count()).select_from(PitConfig)) == 0


async def test_valid_pit_boundaries_and_omitted_values_are_preserved(client):
    csrf = await login(client)
    path = PITS + "/" + "p" * 50
    saved = await client.put(
        path, json={"name": "가" * 100, "total_depth_cm": 123.5}, headers={"X-CSRF-Token": csrf}
    )
    assert saved.status_code == 200
    assert (await client.get(path)).json() == saved.json()
    omitted = await client.put(
        path, json={"name": "Updated", "total_depth_cm": None}, headers={"X-CSRF-Token": csrf}
    )
    assert omitted.status_code == 200 and omitted.json()["total_depth_cm"] == 123.5


def test_admin_settings_openapi_declares_storage_limits():
    api = create_app().openapi()
    fields = api["components"]["schemas"]["AlertSettingsUpdate"]["properties"]
    assert all(fields[field]["maximum"] == 2147483647 for field, _ in FIELDS)
    name = api["components"]["schemas"]["PitConfigUpdateRequest"]["properties"]["name"]
    assert name["anyOf"][0]["maxLength"] == 100
    for method in ["get", "put"]:
        parameter = next(
            p
            for p in api["paths"]["/api/v1/config/pits/{pit_id}"][method]["parameters"]
            if p["name"] == "pit_id"
        )
        assert parameter["schema"]["maxLength"] == 50


async def test_ordinary_validation_response_matches_fastapi_handler():
    request = Request({"type": "http"})
    error = RequestValidationError(
        [
            {
                "loc": ("body", "field"),
                "msg": "Synthetic error",
                "type": "greater_than",
                "input": 3.0,
                "ctx": {"gt": 4.0},
            },
            {
                "loc": ("query", "limit"),
                "msg": "Synthetic limit",
                "type": "int_parsing",
                "input": "invalid",
            },
        ]
    )
    expected = await request_validation_exception_handler(request, error)
    actual = await request_validation_error_response(request, error)
    assert actual.status_code == expected.status_code == 422
    assert actual.body == expected.body
    assert actual.headers == expected.headers


async def test_non_finite_validation_input_and_context_are_json_safe():
    request = Request({"type": "http"})
    error = RequestValidationError(
        [
            {
                "loc": ("body", "field"),
                "msg": "Synthetic error",
                "type": "finite_number",
                "input": {"nested": [float("inf"), float("-inf"), float("nan")]},
                "ctx": {"value": float("inf")},
            },
        ]
    )
    response = await request_validation_error_response(request, error)
    body = json.loads(response.body)
    assert response.status_code == 422
    assert body["detail"][0]["input"]["nested"] == ["inf", "-inf", "nan"]
    assert body["detail"][0]["ctx"]["value"] == "inf"
    json.dumps(body, allow_nan=False)
    RequestValidationErrorResponse.model_validate(body)
