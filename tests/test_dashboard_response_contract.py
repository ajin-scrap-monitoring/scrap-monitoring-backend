"""Validate real error bodies against the models exposed to generated clients."""

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from test_admin_access import RECIPIENT, SETTINGS, login
from test_daily_recordings import RID

from src.app.core.config import get_settings
from src.app.main import create_app
from src.app.schemas import errors as error_schemas
from src.app.schemas.monitoring_v2 import LidarProfileSampleV2, LidarProfileV2
from src.app.services.media_client import media_client


@pytest.fixture(scope="module")
def contract():
    app = create_app()
    return app, app.openapi()


def check_response(contract, response, path, method, status):
    _, schema = contract
    assert response.status_code == status, response.text
    documented = schema["paths"][path][method]["responses"][str(status)]
    declared = documented["content"]["application/json"]["schema"]
    # Follow the published schema, including unions of the two real error envelopes.
    alternatives = declared.get("anyOf", [declared])
    for item in alternatives:
        model = getattr(error_schemas, item["$ref"].rsplit("/", 1)[-1])
        try:
            model.model_validate(response.json())
        except ValidationError:
            continue
        break
    else:
        pytest.fail("Response does not match any declared error schema")


def test_profile_samples_have_named_required_and_optional_fields_without_changing_json(contract):
    _, schema = contract
    model = schema["components"]["schemas"]["LidarProfileV2"]
    reference = model["properties"]["samples"]["items"]["$ref"]
    sample = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    assert set(sample["required"]) == {"positionRatio", "height", "valid"}
    assert set(sample["properties"]) == {
        "positionRatio",
        "height",
        "valid",
        "angleMdeg",
        "sourceIndex",
    }
    assert sample["properties"]["angleMdeg"]["type"] == "integer"
    adapter = TypeAdapter(LidarProfileSampleV2)
    points = [
        {"positionRatio": 0.5, "height": 10.0, "valid": True},
        {
            "positionRatio": None,
            "height": None,
            "valid": False,
            "angleMdeg": 1500,
            "sourceIndex": 7,
        },
    ]
    profile = LidarProfileV2(sensorId="synthetic", referenceDistanceCm=100, samples=points)
    assert profile.model_dump(mode="json")["samples"] == points
    assert "angleMdeg" not in profile.samples[0]
    for invalid in [
        {"height": 10, "valid": True},
        {"positionRatio": 1.5, "height": 10, "valid": True},
        {"positionRatio": 0.5, "height": -1, "valid": True},
    ]:
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_collection_activity_enum_describes_only_emitted_values(contract):
    schema = contract[1]["components"]["schemas"]["CollectionSummaryV2"]
    alternatives = schema["properties"]["activitySource"]["anyOf"]
    values = next(item["enum"] for item in alternatives if "enum" in item)
    assert set(values) == {"CONFIRMED_COMPLETION", "HEIGHT_SETTLED"}
    assert {"type": "null"} in alternatives


async def test_unauthorized_forbidden_and_unknown_source_match_openapi(client, contract):
    response = await client.get("/api/v2/monitoring/profiles")
    check_response(contract, response, "/api/v2/monitoring/profiles", "get", 401)
    await login(client, "operator")
    response = await client.get("/api/v1/settings/alerts")
    check_response(contract, response, "/api/v1/settings/alerts", "get", 403)
    response = await client.get("/api/v2/monitoring/profiles?pit_id=synthetic-missing")
    check_response(contract, response, "/api/v2/monitoring/profiles", "get", 404)


async def test_etag_required_and_stale_and_recipient_conflict_match_openapi(client, contract):
    csrf = await login(client)
    headers = {"X-CSRF-Token": csrf}
    path = "/api/v1/settings/alerts"
    response = await client.put(path, json=SETTINGS, headers=headers)
    check_response(contract, response, path, "put", 428)
    response = await client.put(path, json=SETTINGS, headers={**headers, "If-Match": '"stale"'})
    check_response(contract, response, path, "put", 412)
    path = "/api/v1/notification-recipients"
    assert (await client.post(path, json=RECIPIENT, headers=headers)).status_code == 201
    response = await client.post(path, json=RECIPIENT, headers=headers)
    check_response(contract, response, path, "post", 409)


async def test_both_validation_error_shapes_match_openapi(client, contract):
    await login(client)
    path = "/api/v2/recordings/events"
    response = await client.get(path, params={"page": 0})
    check_response(contract, response, path, "get", 422)
    assert isinstance(response.json()["detail"], list)
    response = await client.get(path, params={"before_seconds": 0, "after_seconds": 0})
    check_response(contract, response, path, "get", 422)
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("status", [404, 409, 410, 429, 502, 503, 504])
async def test_media_errors_preserve_their_envelope_and_hide_upstream_text(
    client, contract, monkeypatch, status
):
    monkeypatch.setattr(get_settings(), "MEDIA_DAILY_SERVICE_URL", "http://daily.test/api/v2")

    def handle(request):
        if status == 504:
            raise httpx.ReadTimeout("private-upstream-text", request=request)
        if status == 502:
            return httpx.Response(200, json={"private-invalid": "private-upstream-text"})
        return httpx.Response(
            status,
            json={
                "error": {"code": "DAILY_RECORDING_NOT_FOUND", "message": "private-upstream-text"}
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as mock:
        monkeypatch.setattr(media_client, "_client", mock)
        await login(client)
        response = await client.get(f"/api/v2/recordings/daily/{RID}")
    check_response(contract, response, "/api/v2/recordings/daily/{recording_id}", "get", status)
    assert "error" in response.json() and "detail" not in response.json()
    assert "private-upstream-text" not in response.text


async def test_unconfigured_media_and_session_validation_remain_documented(
    client, contract, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "")
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "")
    response = await client.post("/api/v1/session", json={"password": "synthetic-secret"})
    check_response(contract, response, "/api/v1/session", "post", 422)
    assert "synthetic-secret" not in response.text
    await login(client)
    response = await client.get("/api/v2/recordings/daily")
    check_response(contract, response, "/api/v2/recordings/daily", "get", 503)


def test_error_statuses_are_specific_to_permissions_and_storage_contracts(contract):
    paths = contract[1]["paths"]
    read = paths["/api/v1/settings/alerts"]["get"]["responses"]
    save = paths["/api/v1/settings/alerts"]["put"]["responses"]
    assert {"401", "403", "422", "503"} <= set(read)
    assert {"412", "428"} <= set(save)
    assert "428" not in read and "412" not in read
    recipient = paths["/api/v1/notification-recipients/{recipientId}"]["patch"]["responses"]
    assert {"404", "409", "412"} <= set(recipient) and "428" not in recipient
    pits = paths["/api/v1/config/pits"]["get"]["responses"]
    assert "401" in pits and "403" not in pits
    events = paths["/api/v2/recordings/events"]["get"]["responses"]
    assert {"409", "410", "503"} <= set(events)
    # Snapshot conflicts and expiration use backend errors, without contacting media.
    for status in ("409", "410"):
        declared = events[status]["content"]["application/json"]["schema"]
        assert declared["$ref"].endswith("/HTTPErrorResponse")
    for path in ["/api/v2/recordings/daily/{recording_id}", "/api/v1/media/clips/{clip_id}"]:
        assert {"401", "403", "409", "410", "503"} <= set(paths[path]["delete"]["responses"])
