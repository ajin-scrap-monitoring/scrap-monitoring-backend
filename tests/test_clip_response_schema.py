"""Published clip responses stay typed while legacy optional time is omitted."""

import pytest
from test_media_control import FakeMedia, fake_media, login, seed_alert

from src.app.main import create_app
from src.app.schemas.media_service import ClipDetail, ClipReceipt, ClipRequest

__all__ = ["fake_media"]


def resolve(schema, node):
    while "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


@pytest.mark.parametrize("model", [ClipReceipt, ClipDetail, ClipRequest])
def test_serialization_schema_keeps_identity_and_timestamp_properties(model):
    schema = model.model_json_schema(mode="serialization")
    properties = schema["properties"]
    assert {"event_id", "camera_id", "event_at"} <= set(properties)
    time = properties["event_at"]
    assert {"type": "string", "format": "date-time"} in time["anyOf"]
    assert {"type": "null"} in time["anyOf"]
    assert "event_at" not in schema["required"]
    if model is not ClipRequest:
        assert {
            "clip_id",
            "status",
            "accepted_at",
            "requested_start_at",
            "requested_end_at",
        } <= set(schema["required"])
        assert properties["clip_id"]["format"] == "uuid"
    if model is ClipDetail:
        assert {
            "coverage",
            "gaps",
            "content_path",
            "recording_policy",
            "actual_start_at",
            "actual_end_at",
        } <= set(properties)


@pytest.mark.parametrize("status", ["200", "202"])
def test_public_create_response_points_to_typed_receipt(status):
    schema = create_app().openapi()
    response = schema["paths"]["/api/v1/media/clips"]["post"]["responses"][status]
    receipt = resolve(schema, response["content"]["application/json"]["schema"])
    assert {"clip_id", "event_id", "camera_id", "status", "event_at"} <= set(receipt["properties"])


def test_detail_and_nested_job_responses_keep_typed_fields():
    schema = create_app().openapi()
    response = schema["paths"]["/api/v1/media/clips/{clip_id}"]["get"]["responses"]["200"]
    detail = resolve(schema, response["content"]["application/json"]["schema"])
    assert {"clip_id", "coverage", "gaps", "content_path", "event_at"} <= set(detail["properties"])
    response = schema["paths"]["/api/v1/media/clip-jobs"]["get"]["responses"]["200"]
    page = response["content"]["application/json"]["schema"]
    job = resolve(schema, page["items"])
    clip = job["properties"]["clip"]
    variants = [resolve(schema, item) for item in clip["anyOf"] if item.get("type") != "null"]
    assert len(variants) == 2
    assert all(
        {"clip_id", "event_id", "status", "event_at"} <= set(x["properties"]) for x in variants
    )


def test_legacy_null_time_omission_does_not_drop_other_nullable_fields():
    fake = FakeMedia()
    payload = ClipDetail.model_validate(fake.detail()).model_dump(mode="json")
    assert "event_at" not in payload
    assert payload["actual_start_at"] is None and payload["completed_at"] is None
    assert "coverage" in payload and payload["coverage"] is None
    assert "content_path" in payload and payload["content_path"] is None
    request = ClipRequest(event_id="synthetic")
    assert set(request.model_dump(mode="json")) == {"event_id", "camera_id"}


async def test_legacy_http_receipt_matches_published_required_fields(
    client, test_db_session, fake_media
):
    identifier = await seed_alert(test_db_session)
    response = await client.post(
        "/api/v1/media/clips", json={"event_id": identifier}, headers=await login(client)
    )
    assert response.status_code == 202, response.text
    schema = create_app().openapi()
    published = schema["paths"]["/api/v1/media/clips"]["post"]["responses"]["202"]
    receipt = resolve(schema, published["content"]["application/json"]["schema"])
    assert set(receipt["required"]) <= set(response.json())
    assert "event_at" not in response.json()
