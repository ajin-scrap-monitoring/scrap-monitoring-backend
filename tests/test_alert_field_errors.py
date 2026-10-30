"""Cross-field errors identify the pre-alert input without changing saved policy."""

import pytest
from test_admin_access import login

from src.app.schemas.errors import RequestValidationErrorResponse

PATH = "/api/v1/settings/alerts?pit_id=pit-demo"


@pytest.mark.parametrize("critical,warning", [(3, 99), (90, 89), (4, 0), (90, 85.000001)])
@pytest.mark.parametrize("enabled", [False, True])
async def test_cross_field_code_and_location_preserve_saved_settings(
    client, critical, warning, enabled
):
    csrf = await login(client)
    original = await client.get(PATH)
    payload = {k: v for k, v in original.json().items() if k != "version"}
    payload.update(
        collectionThresholdPercent=critical,
        preCollectionAlert={"enabled": enabled, "thresholdPercent": warning},
    )
    response = await client.put(
        PATH, json=payload, headers={"X-CSRF-Token": csrf, "If-Match": original.headers["etag"]}
    )
    assert response.status_code == 422
    body = response.json()
    RequestValidationErrorResponse.model_validate(body)
    assert len(body["detail"]) == 1
    issue = body["detail"][0]
    assert issue["type"] == "alert_threshold_gap"
    assert issue["loc"] == ["body", "preCollectionAlert", "thresholdPercent"]
    assert issue["input"] == warning
    assert issue["ctx"] == {"minimumGap": 5, "collectionThresholdPercent": critical}
    current = await client.get(PATH)
    assert current.json() == original.json() and current.headers["etag"] == original.headers["etag"]


async def test_individual_field_validation_keeps_its_own_error_code(client):
    csrf = await login(client)
    original = await client.get(PATH)
    payload = {k: v for k, v in original.json().items() if k != "version"}
    payload["collectionThresholdPercent"] = -1
    response = await client.put(
        PATH, json=payload, headers={"X-CSRF-Token": csrf, "If-Match": original.headers["etag"]}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "greater_than_equal"
    assert response.json()["detail"][0]["loc"] == ["body", "collectionThresholdPercent"]
