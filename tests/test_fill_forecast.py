"""Public synthetic trends, real HTTP contracts, and unavailable-data boundaries."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_monitoring_v2 import NOW, assessment, login, monitoring_config, send, status  # noqa: F401

from src.app.models.config import PitConfig
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.media_job import MediaClipJob
from src.app.models.metric import ScrapMetric
from src.app.schemas.fill_forecast import FillForecastV2
from src.app.schemas.load_history_v2 import HistorySampleV2
from src.app.services.fill_forecast_service import fill_forecast, project_trend, recent_points
from src.app.services.monitoring_v2_service import monitoring_source

URL = "/api/v2/monitoring/fill-forecast?pit_id=pit-demo"


@pytest.fixture(autouse=True)
def forecast_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("src.app.services.fill_forecast_service.datetime", Clock)


def result(current=70, target=90):
    return FillForecastV2(
        pitId="pit-demo",
        computedAt=NOW,
        validUntil=NOW + timedelta(seconds=30),
        inputSource="EDGE_STATE",
        currentLoadPercent=current,
        targetThresholdPercent=target,
        windowFrom=NOW - timedelta(minutes=10),
        windowTo=NOW,
    )


def point(second, value, **changes):
    at = NOW + timedelta(seconds=second)
    return HistorySampleV2(
        bucketStartAt=at,
        bucketEndAt=at + timedelta(seconds=1),
        generatedAt=at,
        observedAt=at,
        valid=True,
        valuePercent=value,
        dataState="AVAILABLE",
        methodVersion="method-1",
        **changes,
    )


def suffix(samples, cutoff=None):
    return recent_points(
        samples, "EDGE_STATE", "method-1", False, cutoff or NOW - timedelta(minutes=10), NOW
    )


async def test_forecast_auth_scope_no_store_and_missing_data(client):
    assert (await client.get(URL)).status_code == 401
    await login(client)
    assert (await client.get(URL.replace("pit-demo", "other"))).status_code == 404
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["state"] == "UNAVAILABLE"
    assert data["expectedThresholdAt"] is data["remainingSeconds"] is None
    assert "CURRENT_LOAD_UNAVAILABLE" in data["reasonCodes"]


@pytest.mark.parametrize("target,remaining", [(85, 90), (90, 120)])
async def test_http_recent_increase_uses_actual_threshold_and_writes_no_operations(
    client, test_db_session, target, remaining
):
    test_db_session.add(
        PitConfig(id="pit-demo", name="Synthetic", critical_threshold_percent=target)
    )
    await test_db_session.commit()
    await send(client, status())
    for i, percent in enumerate([50, 60, 70]):
        await send(
            client, assessment(f"trend-{i}", percent / 100, NOW - timedelta(seconds=120 - i * 60))
        )
    await login(client)
    data = (await client.get(URL)).json()
    assert data["state"] == "AVAILABLE", data
    assert data["currentLoadPercent"] == 70
    assert data["targetThresholdPercent"] == target
    assert data["remainingSeconds"] == remaining
    assert datetime.fromisoformat(data["expectedThresholdAt"]) == NOW + timedelta(seconds=remaining)
    assert datetime.fromisoformat(data["validUntil"]) <= NOW + timedelta(seconds=30)
    assert data["sampleCount"] == 3 and data["increasePercentPerMinute"] == 10
    for model in (AlertLog, CollectionLog, ScrapMetric, MediaClipJob):
        assert await test_db_session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("percent", [90, 95])
async def test_reached_threshold_is_real_zero_remaining(client, test_db_session, percent):
    await send(client, status())
    await send(client, assessment("at-threshold", percent / 100))
    data = await fill_forecast(test_db_session, monitoring_source("pit-demo"), NOW)
    assert data.state == "REACHED" and data.remainingSeconds == 0
    assert data.expectedThresholdAt == NOW


async def test_stale_measurement_never_publishes_an_estimate(client, test_db_session):
    await send(client, status())
    await send(client, assessment("stale", 0.7, NOW - timedelta(seconds=31)))
    data = await fill_forecast(test_db_session, monitoring_source("pit-demo"), NOW)
    assert data.state == "UNAVAILABLE" and data.expectedThresholdAt is None
    assert data.validUntil == NOW


@pytest.mark.parametrize(
    "values,reason",
    [
        ([70], "INSUFFICIENT_TREND_SAMPLES"),
        ([70, 70, 70], "LOAD_NOT_INCREASING"),
        ([72, 71, 70], "LOAD_NOT_INCREASING"),
        ([60, 75, 65, 70], "UNSTABLE_LOAD_TREND"),
        ([69.998, 69.999, 70], "FORECAST_HORIZON_EXCEEDED"),
    ],
)
def test_unsupported_trends_remain_null(values, reason):
    data = result()
    project_trend(
        data,
        [(NOW + timedelta(seconds=(i - len(values) + 1) * 60), v) for i, v in enumerate(values)],
    )
    assert data.state == "UNAVAILABLE" and data.reasonCodes == [reason]
    assert data.remainingSeconds is data.expectedThresholdAt is None


def test_short_span_and_irregular_actual_times():
    data = result()
    project_trend(data, [(NOW + timedelta(seconds=i), 68 + i) for i in [-2, -1, 0]])
    assert data.reasonCodes == ["INSUFFICIENT_TREND_SPAN"]
    data = result(current=0.5, target=1)
    project_trend(data, [(NOW + timedelta(seconds=i), 0.5 + i / 1000) for i in [-100, -60, 0]])
    assert data.state == "AVAILABLE" and data.remainingSeconds == 500


@pytest.mark.parametrize(
    "break_kind",
    ["invalid", "null_time", "gap", "drop", "method", "mixed", "simulation", "invalid_in_bucket"],
)
def test_suffix_never_crosses_missing_drop_or_method_boundaries(break_kind):
    samples = [point(-180, 30), point(-120, 40), point(-60, 50), point(0, 60)]
    if break_kind == "invalid":
        samples[2].valid = False
    elif break_kind == "null_time":
        samples[2].observedAt = None
    elif break_kind == "gap":
        samples.pop(2)
    elif break_kind == "drop":
        samples[2].valuePercent = 5
        samples[3].valuePercent = 6
    elif break_kind == "method":
        samples[2].methodVersion = "old-method"
    elif break_kind == "mixed":
        samples[2].inputSource = "MIXED"
    elif break_kind == "simulation":
        samples[2].simulated = True
    else:
        samples[2].invalidSamples = 1
    assert len(suffix(samples)) <= 2
    assert (
        len(
            suffix(
                [point(-120, 30), point(-60, 40), point(0, 50)], cutoff=NOW - timedelta(seconds=30)
            )
        )
        == 1
    )
