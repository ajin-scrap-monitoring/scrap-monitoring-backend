"""Response schemas require emitted defaults while internal model defaults remain usable."""

from datetime import UTC, datetime

import pytest
from test_monitoring_v2 import login
from test_monitoring_v2 import monitoring_config as monitoring_config

from src.app.main import create_app
from src.app.schemas.active_alerts_v2 import ActiveFillAlertPageV2
from src.app.schemas.monitoring_v2 import LidarProfileV2


@pytest.mark.parametrize(
    "model,field,values",
    [
        (
            ActiveFillAlertPageV2,
            "coverage",
            {"pitId": "pit-demo", "computedAt": datetime.now(UTC), "evaluationState": "UNKNOWN"},
        ),
        (LidarProfileV2, "samples", {"sensorId": "lidar-a", "referenceDistanceCm": 100}),
    ],
)
def test_serialized_defaults_are_required_but_internal_defaults_stay_optional(model, field, values):
    value = model(**values)
    assert value.model_dump(mode="json")[field] == []
    response = model.model_json_schema(mode="serialization")
    assert field in response["required"]
    assert set(response["required"]) == {
        *model.model_json_schema(mode="validation")["required"],
        field,
    }
    assert field not in model.model_json_schema(mode="validation")["required"]


def test_actual_openapi_response_references_require_coverage_and_samples():
    schema = create_app().openapi()
    components = schema["components"]["schemas"]
    for path, field in [("active-alerts", "coverage"), ("profiles", "samples")]:
        ref = schema["paths"][f"/api/v2/monitoring/{path}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        model = components[ref["$ref"].rsplit("/", 1)[-1]]
        if path == "profiles":
            item = model["properties"]["profiles"]["items"]
            model = components[item["$ref"].rsplit("/", 1)[-1]]
        assert field in model["required"]
        assert model["properties"][field]["type"] == "array"


@pytest.mark.parametrize("scope", [None, "FILL_OPERATIONS", "ALL"])
async def test_real_response_always_includes_coverage(client, scope):
    await login(client)
    params = {"pit_id": "pit-demo"}
    if scope is not None:
        params["scope"] = scope
    response = await client.get("/api/v2/monitoring/active-alerts", params=params)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["coverage"], list)
    if scope == "ALL":
        assert {c["componentKind"] for c in body["coverage"]} == {
            "EDGE",
            "LIDAR",
            "MEASUREMENT",
            "CAMERA",
        }
    else:
        assert body["coverage"] == []


async def test_real_profiles_always_include_empty_samples_without_measurements(client):
    await login(client)
    response = await client.get("/api/v2/monitoring/profiles", params={"pit_id": "pit-demo"})
    assert response.status_code == 200
    profiles = response.json()["profiles"]
    assert len(profiles) == 2
    assert all(p["samples"] == [] for p in profiles)
