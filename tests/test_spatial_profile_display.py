"""Spatial rendering cannot mutate source samples or operational statistics."""

from itertools import pairwise

import pytest
from test_monitoring_v2 import login
from test_monitoring_v2 import monitoring_config as monitoring_config

from src.app.api import monitoring_v2 as api
from src.app.schemas.monitoring_v2 import LidarProfilePageV2, LidarProfileV2
from src.app.services.spatial_profile_display import DISPLAY_BINS, spatial_profile_display


def profile(samples, **overrides):
    return LidarProfileV2(
        **{
            "sensorId": "lidar-a",
            "referenceDistanceCm": 1000,
            "sampleSource": "FILTERED_POINT_CLOUD",
            "valuePolicy": "SENSOR_ANGLE",
            "coverageBasis": "RECEIVED_POINTS",
            "coverageRatio": 0.75,
            "average": 123,
            "minimum": 10,
            "maximum": 200,
            "filteredAverage": 120,
            "sectionFillPercent": 12,
            "summaryValuePolicy": "SCAN_MEAN_TEMPORAL_MEDIAN",
            "samples": samples,
            **overrides,
        }
    )


def point(x, y, valid=True):
    return {"positionRatio": x, "height": y, "valid": valid}


def page(*profiles):
    return LidarProfilePageV2(pitId="pit-demo", profiles=list(profiles), meanSectionFillPercent=12)


def test_folding_and_duplicate_points_have_one_median_per_spatial_bin():
    original = page(profile([point(0.8, 200), point(0.401, 10), point(0.402, 100), point(0.1, 0)]))
    saved = original.model_dump()
    result = spatial_profile_display(original)
    samples = result.profiles[0].samples
    assert len(samples) == DISPLAY_BINS
    assert all(a["positionRatio"] < b["positionRatio"] for a, b in pairwise(samples))
    assert samples[80]["height"] == 55
    assert samples[20]["height"] == 0 and samples[20]["valid"]
    assert samples[160]["height"] == 200
    assert original.model_dump() == saved
    assert result.profiles[0].model_dump(
        exclude={
            "samples",
            "valuePolicy",
            "displayBinCount",
            "displayCoverageRatio",
            "displayUnpositionedCount",
        }
    ) == original.profiles[0].model_dump(
        exclude={
            "samples",
            "valuePolicy",
            "displayBinCount",
            "displayCoverageRatio",
            "displayUnpositionedCount",
        }
    )
    assert result.meanSectionFillPercent == original.meanSectionFillPercent


def test_spatial_gaps_are_not_interpolated_and_located_invalid_samples_block_their_bin():
    result = spatial_profile_display(
        page(
            profile(
                [
                    point(0.1, 0),
                    point(0.501, 100),
                    point(0.502, None, False),
                    point(None, None, False),
                    point(0.9, 200),
                ]
            )
        )
    ).profiles[0]
    assert result.samples[100] == point(0.5025, None, False)
    assert not result.samples[80]["valid"] and result.samples[80]["height"] is None
    assert result.displayUnpositionedCount == 1
    assert result.displayCoverageRatio == 2 / DISPLAY_BINS
    assert result.coverageRatio == 0.75 and result.coverageBasis == "RECEIVED_POINTS"
    assert all("angleMdeg" not in x and "sourceIndex" not in x for x in result.samples)


def test_sensor_series_are_independent_and_order_does_not_change_spatial_results():
    points = [point(1, 100), point(0.2, 10), point(0.201, 30), point(0, 0)]
    a = profile(points)
    b = profile([point(0.2, 90)], sensorId="lidar-b")
    result = spatial_profile_display(page(a, b))
    reverse = spatial_profile_display(page(profile(list(reversed(points)))))
    assert result.profiles[0].samples == reverse.profiles[0].samples
    assert result.profiles[0].samples[40]["height"] == 20
    assert result.profiles[1].samples[40]["height"] == 90
    assert result.profiles[0].samples[-1]["height"] == 100


def test_missing_profile_stays_empty_and_invalid_scan_cannot_borrow_previous_values():
    original = page(profile([]))
    assert spatial_profile_display(original).model_dump() == original.model_dump()
    missing = spatial_profile_display(page(profile([point(None, None, False)]))).profiles[0]
    assert not any(sample["valid"] for sample in missing.samples)
    assert all(sample["height"] is None for sample in missing.samples)
    assert missing.displayUnpositionedCount == 1 and missing.displayCoverageRatio == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"sampleSource": "MEASUREMENT"},
        {"valuePolicy": "SPATIAL_TEMPORAL_MEDIAN"},
        {"sampleSource": None, "sectionSource": "EDGE_SECTION_PROFILE"},
    ],
)
def test_other_profile_policies_are_preserved(overrides):
    original = page(profile([point(0.4, 50)], **overrides))
    assert spatial_profile_display(original).model_dump() == original.model_dump()


async def test_api_default_is_spatial_and_source_view_retains_original_samples(client, monkeypatch):
    source = page(profile([point(0.8, 100), point(None, None, False), point(0.2, 200)]))
    before = source.model_dump()
    calls = []

    async def snapshot(*args):
        calls.append(True)
        return source

    monkeypatch.setattr(api, "lidar_profile_snapshot", snapshot)
    url = "/api/v2/monitoring/profiles?pit_id=pit-demo"
    assert (await client.get(url)).status_code == 401
    await login(client)
    display = await client.get(url)
    assert display.status_code == 200 and display.headers["cache-control"] == "no-store"
    assert display.json()["profiles"][0]["displayBinCount"] == DISPLAY_BINS
    raw = await client.get(url + "&view=source")
    assert raw.status_code == 200 and raw.headers["cache-control"] == "no-store"
    assert raw.json()["profiles"][0]["samples"] == source.profiles[0].samples
    assert "displayBinCount" not in raw.json()["profiles"][0]
    assert source.model_dump() == before
    assert (await client.get(url + "&view=unsupported")).status_code == 422
    assert len(calls) == 2
