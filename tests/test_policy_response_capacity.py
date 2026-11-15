"""Successful policy writes keep their public contract and safe operating evidence."""

import copy
import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.app.core.config import get_settings
from src.app.schemas.daily_recording import DailyPolicy
from src.app.schemas.media_service import RecordingPolicy
from src.app.services.daily_recording_service import DailyRecordingService
from src.app.services.media_client import MediaClient, MediaServiceError

POLICIES = [
    (DailyPolicy, {"enabled": True, "retention_days": 30}),
    (RecordingPolicy, {"pre_roll_seconds": 30, "post_roll_seconds": 30, "retention_days": 30}),
]


def validation(status="insufficient_capacity"):
    return {
        "status": status,
        "evaluated_at": "2026-01-01T00:00:00Z",
        "policy_applied": True,
        "warnings": [{"code": "RETENTION_CAPACITY_EXCEEDED", "message": "private-secret"}],
        "current": {"internal_path": "private-secret"},
        "retention": {"private-secret": 999},
    }


@pytest.mark.parametrize("schema,fields", POLICIES)
@pytest.mark.parametrize("status", [None, "insufficient_capacity", "estimated_fit", "unknown"])
async def test_put_returns_only_policy_and_logs_safe_warning(
    monkeypatch, caplog, schema, fields, status
):
    settings = get_settings()
    monkeypatch.setattr(settings, "MEDIA_SERVICE_URL", "http://media.test/api/v1")
    monkeypatch.setattr(settings, "MEDIA_DAILY_SERVICE_URL", "http://media.test/api/v2")
    monkeypatch.setattr(settings, "MEDIA_API_TOKEN", SecretStr("synthetic-token"))
    requests = []
    body = dict(fields)
    if status is not None:
        body["capacity_validation"] = validation(status)
        body["capacity_validation"]["evaluated_at"] = "2026-01-01T09:00:00+09:00"

    async def handle(request):
        requests.append(request)
        assert request.method == "PUT" and json.loads(request.content) == fields
        return httpx.Response(200, json=body)

    caplog.set_level(logging.WARNING)
    client = MediaClient()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        client._client = upstream
        result = (
            await DailyRecordingService(client).policy(schema(**fields))
            if schema is DailyPolicy
            else await client.put_policy("camera-main", schema(**fields))
        )
    assert result.model_dump() == fields and len(requests) == 1
    assert "private-secret" not in caplog.text
    if status is not None:
        assert f"status={status}" in caplog.text
        assert "evaluated_at=2026-01-01T00:00:00+00:00" in caplog.text
        assert "RETENTION_CAPACITY_EXCEEDED" in caplog.text and "applied=true" in caplog.text
    else:
        assert "RECORDING_POLICY_CAPACITY" not in caplog.text


@pytest.mark.parametrize("schema,fields", POLICIES)
@pytest.mark.parametrize(
    "bad", ["not-applied", "null", "status", "time", "code", "policy", "extra", "non-object"]
)
def test_malformed_or_unapplied_evidence_is_not_success(caplog, schema, fields, bad):
    body = {**fields, "capacity_validation": validation()}
    if bad == "not-applied":
        body["capacity_validation"]["policy_applied"] = False
    elif bad == "null":
        body["capacity_validation"] = None
    elif bad == "status":
        body["capacity_validation"]["status"] = "private-secret"
    elif bad == "time":
        body["capacity_validation"]["evaluated_at"] = "2026-01-01T00:00:00"
    elif bad == "code":
        body["capacity_validation"]["warnings"][0]["code"] = "private-secret\nINJECTED"
    elif bad == "policy":
        body["retention_days"] = 0
    elif bad == "extra":
        body["private-secret"] = 1
    else:
        body = []
    before = copy.deepcopy(body)
    with pytest.raises(MediaServiceError) as error:
        MediaClient()._parse_policy_update(httpx.Response(200, json=body), schema)
    assert (error.value.status_code, error.value.code) == (502, "MEDIA_BAD_RESPONSE")
    assert body == before and "private-secret" not in str(error.value)
    assert "RECORDING_POLICY_CAPACITY" not in caplog.text


@pytest.mark.parametrize("schema,fields", POLICIES)
def test_get_and_request_remain_strict(schema, fields):
    body = {**fields, "capacity_validation": validation()}
    with pytest.raises(MediaServiceError):
        MediaClient()._parse(httpx.Response(200, json=body), schema)
    with pytest.raises(ValidationError):
        schema.model_validate(body)


def test_estimated_fit_without_warnings_is_not_logged_as_guarantee(caplog):
    evidence = validation("estimated_fit")
    evidence["warnings"] = []
    result = MediaClient()._parse_policy_update(
        httpx.Response(
            200, json={"enabled": True, "retention_days": 30, "capacity_validation": evidence}
        ),
        DailyPolicy,
    )
    assert result.retention_days == 30 and not caplog.records
