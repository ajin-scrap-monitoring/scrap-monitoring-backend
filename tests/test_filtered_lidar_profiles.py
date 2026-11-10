"""Invented point clouds: graph-only provenance, strict context, and scan-mean medians."""

import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.app.core.config import get_settings
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.schemas.edge_v2 import Assessment, Configuration, DeviceStatus, StatusReport
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.filtered_lidar_profiles import project_cloud
from src.app.services.monitoring_v2_service import build_snapshot, lidar_profiles, monitoring_source
from tests import test_simulation_height as legacy
from tests.test_edge_section_results import judgment
from tests.test_edge_world_height import REFERENCE, world_inputs
from tests.test_edge_world_height import world_context as world_context
from tests.test_filtered_point_cloud import cloud_state
from tests.test_filtered_point_cloud import settings as cloud_settings
from tests.test_height_fill_pipeline import START, send
from tests.test_height_fill_pipeline import height_context as height_context
from tests.test_monitoring_v2 import login, status
from tests.test_sensor_angle_height import angle_context as angle_context


def inputs(second=0, height=50.0125):
    c, _ = world_inputs()
    j = judgment()
    j["point_cloud"] = cloud_settings()
    j["window"] = {"scan_count": 2, "min_scans": 1, "max_age_ms": 800, "pair_tolerance_ms": 50}
    c["judgment"] = j
    a = cloud_state()
    now = START + timedelta(seconds=second)
    a.update(
        message_id=f"filtered-{second}",
        assessment_id=f"cloud-assessment-{second}",
        generated_at=now.isoformat(),
    )
    for suffix in ["a", "b"]:
        sid = "lidar-" + suffix
        cl = next(x for x in a["filtered_point_clouds"] if x["sensor_id"] == sid)
        ref = cl["source_scans"][0]
        ref.update(calibration_version="edge-cal-" + suffix, scan_id=f"cloud-{suffix}-{second}")
        cl.update(
            calibration_version=ref["calibration_version"],
            valid_point_count=2,
            valid=[True, True],
            invalid_reason_codes=[[], []],
            sample_counts=[1, 1],
            xyz_mm=[
                [(20 + (100 - height) * 0.75) * 10, 500, height * 10],
                [(20 + (100 - height) * 0.75) * 10, 700, height * 10],
            ],
        )
        cl["anchor"].update(
            scan_id=ref["scan_id"],
            acquired_at=now.isoformat(),
            sequence=second,
            acquired_monotonic_ns=str((second + 1) * 10**9),
        )
        for r in a["evidence"]["scans"]:
            if r["sensor_id"] == sid:
                r.update(ref)
        for component in a["fusion_result"]["components"]:
            if component["sensor_id"] == sid:
                component["anchor_scan_id"] = ref["scan_id"]
    return c, a


def calculate(change=None):
    c, a = inputs()
    r = copy.deepcopy(REFERENCE)
    s = status(when=START)
    s["config_revision"] = "cfg-1"
    if change:
        change(c, a, r, s)
    config = Configuration.model_validate(c)
    assessment = Assessment.model_validate(a)
    status_result = DeviceStatus(
        site_id="site-demo",
        edge_id="edge-demo",
        connection_state="REPORTING",
        last_received_at=START,
        effective_state=s["overall_state"],
        reason_codes=[],
        snapshot=StatusReport.model_validate(s),
    )
    return project_cloud(
        "lidar-a",
        assessment,
        config,
        SimulationHeightReference.model_validate(r),
        assessment.filtered_point_clouds[0],
        status_result,
        START,
        "pit-demo",
    )


def test_decimal_world_coordinates_are_projected_once_as_cm(world_context):
    profile = calculate()
    assert profile.average == pytest.approx(50.0125)
    assert profile.samples[0]["height"] == pytest.approx(50.0125)
    assert profile.samples[0]["positionRatio"] == pytest.approx(0.25)
    assert [s["sourceIndex"] for s in profile.samples] == [2, 7]
    assert profile.sampleSource == "FILTERED_POINT_CLOUD"
    assert profile.sourceScanId == "cloud-a-0"
    assert profile.calibrationState == "SIMULATION"
    assert "DEFAULT_SETTINGS" in profile.reasonCodes


def test_zero_floor_height_is_valid(world_context):
    def change(c, a, r, s):
        a["filtered_point_clouds"][0]["xyz_mm"] = [[950, 500, 0], [950, 700, 0]]

    p = calculate(change)
    assert p.average == 0 and all(s["valid"] for s in p.samples)


@pytest.mark.parametrize("kind", ["invalid", "outside", "above", "wall"])
def test_invalid_or_outside_points_remain_gaps_without_raw_fallback(world_context, kind):
    def change(c, a, r, s):
        cl = a["filtered_point_clouds"][0]
        if kind == "invalid":
            cl.update(
                valid_point_count=1,
                valid=[True, False],
                invalid_reason_codes=[[], ["LOW_QUALITY"]],
                sample_counts=[1, 0],
            )
            cl["xyz_mm"][1] = None
        else:
            cl["xyz_mm"][1] = {
                "outside": [575, 3000, 500],
                "above": [575, 700, 1100],
                "wall": [575, 10, 500],
            }[kind]

    p = calculate(change)
    assert len(p.samples) == 2 and p.samples[1]["height"] is None
    assert p.samples[1]["positionRatio"] is None and not p.samples[1]["valid"]
    assert p.coverageRatio == 0.5


@pytest.mark.parametrize(
    "kind",
    [
        "revision",
        "frame",
        "calibration",
        "pose",
        "stale",
        "future",
        "status",
        "sensor",
        "window",
        "filter",
        "angles",
    ],
)
def test_incompatible_cloud_context_never_becomes_a_graph(world_context, kind):
    def change(c, a, r, s):
        cl = a["filtered_point_clouds"][0]
        if kind == "revision":
            r["config_revision"] = "other"
        elif kind == "frame":
            r["coordinate_frame_id"] = "other"
        elif kind == "calibration":
            r["calibration_versions"]["lidar-a"] = "other"
        elif kind == "pose":
            r["sensor_poses"]["lidar-a"]["origin_cm"][0] += 1
        elif kind == "stale":
            cl["anchor"]["acquired_at"] = (START - timedelta(seconds=31)).isoformat()
        elif kind == "future":
            a["generated_at"] = (START + timedelta(seconds=1)).isoformat()
        elif kind == "status":
            s["overall_state"] = "CONFIG_ERROR"
        elif kind == "sensor":
            s["sensors"][0]["data_state"] = "STALE"
        elif kind == "window":
            c["judgment"]["window"]["max_age_ms"] = 700
        elif kind == "filter":
            c["judgment"]["point_cloud"]["quality_min"] = 9
        else:
            c["sensors"][0]["angle_intervals_mdeg"] = [
                {"start_inclusive": 0, "end_exclusive": 1000}
            ]

    p = calculate(change)
    assert p.calibrationState == "UNAVAILABLE" and not p.samples and p.average is None


@pytest.fixture
def cloud_context(world_context, monkeypatch):
    monkeypatch.setenv("EDGE_V2_FILTERED_PROFILES_ENABLED", "true")
    get_settings.cache_clear()
    yield world_context
    get_settings.cache_clear()


async def prepare(client, db):
    # Reuse the toy pit setup but register this test's complete configuration once.
    original = legacy.raw_inputs
    try:
        legacy.raw_inputs = lambda: (inputs()[0], world_inputs()[1])
        await legacy.prepare(client, db)
    finally:
        legacy.raw_inputs = original


async def frame(client, db, clock, second, height=50.0125):
    await legacy.frame(client, db, clock, second, 20)
    _, a = inputs(second, height)
    response = await send(client, a)
    assert response.status_code == 201, response.text
    return a, await lidar_profiles(db, monitoring_source("pit-demo"), clock["now"])


async def test_existing_api_selects_filtered_points_and_separate_five_second_scan_mean(
    client, test_db_session, cloud_context
):
    db = test_db_session
    await prepare(client, db)
    await login(client)
    for second in range(7):
        a, page = await frame(client, db, cloud_context, second, 90 if second == 6 else 50.0125)
    assert page.profiles[0].average == 90
    assert page.profiles[0].filteredAverage == pytest.approx(50.0125)
    assert page.meanSectionFillPercent == pytest.approx(50.0125)
    assert all(p.summaryValuePolicy == "SCAN_MEAN_TEMPORAL_MEDIAN" for p in page.profiles)
    response = await client.get("/api/v2/monitoring/profiles?pit_id=pit-demo")
    assert response.status_code == 200
    assert response.json()["profiles"][0]["sampleSource"] == "FILTERED_POINT_CLOUD"
    # Raw measurement means still drive operational fill, independent of this graph.
    snapshot = await build_snapshot(db, monitoring_source("pit-demo"), cloud_context["now"])
    assert snapshot.summary.loadPercent == pytest.approx(20, abs=0.05)
    raw = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "measurement"))
    assert raw.payload["sensors"][0]["scan"]["points"][0]["position_mm"]["z"] == 200
    before = copy.deepcopy(
        (await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))).payload
    )
    assert (await send(client, a)).json()["duplicate"] is True
    assert (
        await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    ).payload == before
    receipt = await db.scalar(
        select(EdgeV2Message).where(EdgeV2Message.message_id == a["message_id"])
    )
    assert receipt.compact


@pytest.mark.parametrize("kind", ["missing", "null", "unavailable", "stale"])
async def test_current_unusable_state_does_not_borrow_raw_or_previous_cloud(
    client, test_db_session, cloud_context, kind
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, cloud_context, second)
    await legacy.frame(client, db, cloud_context, 5, 20)
    _, a = inputs(5)
    if kind == "missing":
        a.pop("filtered_point_clouds")
    elif kind == "null":
        a["filtered_point_clouds"] = None
    elif kind == "unavailable":
        cl = a["filtered_point_clouds"][0]
        cl.update(
            state="UNAVAILABLE",
            reason_codes=["NO_VALID_POINTS", "DEFAULT_SETTINGS"],
            valid_point_count=0,
            valid=[False, False],
            xyz_mm=[None, None],
            invalid_reason_codes=[["LOW_QUALITY"], ["LOW_QUALITY"]],
            sample_counts=[0, 0],
        )
    else:
        for cl in a["filtered_point_clouds"]:
            cl["anchor"]["acquired_at"] = (START - timedelta(seconds=60)).isoformat()
    r = await send(client, a)
    assert r.status_code == 201, r.text
    page = await lidar_profiles(db, monitoring_source("pit-demo"), cloud_context["now"])
    assert page.profiles[0].average is None and not page.profiles[0].samples
    assert page.profiles[0].sectionFillPercent is None
    assert all(p.sampleSource == "FILTERED_POINT_CLOUD" for p in page.profiles)
    if kind == "unavailable":
        assert page.profiles[1].average is not None
    window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "filtered_profile_window"))
    assert window.payload["sensors"]["lidar-a"]["frames"] == []
    raw_window = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "height_window"))
    assert len(raw_window.payload["sensors"]["lidar-a"]["frames"]) == 5


async def test_status_failure_invalidates_filtered_window_without_clearing_raw_receipts(
    client, test_db_session, cloud_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, cloud_context, second)
    s = status("failure", START + timedelta(seconds=5), 100)
    s["config_revision"] = "cfg-1"
    s["overall_state"] = "CONFIG_ERROR"
    cloud_context["now"] = START + timedelta(seconds=5)
    assert (await send(client, s)).status_code == 201
    row = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "filtered_profile_window"))
    assert row.payload == {}
    page = await lidar_profiles(db, monitoring_source("pit-demo"), cloud_context["now"])
    assert all(not p.samples for p in page.profiles)
    assert (await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "state"))).payload[
        "filtered_point_clouds"
    ]


async def test_old_state_does_not_replace_current_graph_or_advance_window(
    client, test_db_session, cloud_context
):
    db = test_db_session
    await prepare(client, db)
    for second in range(5):
        await frame(client, db, cloud_context, second)
    row = await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "filtered_profile_window"))
    before = copy.deepcopy(row.payload)
    _, a = inputs(1, 80)
    a.update(message_id="late-unique", assessment_id="late-unique")
    # Give the delayed result an independent scan identity to avoid an artifact conflict.
    for cl in a["filtered_point_clouds"]:
        cl["anchor"]["scan_id"] += "-late"
        cl["source_scans"][0]["scan_id"] += "-late"
    for ref in a["evidence"]["scans"]:
        ref["scan_id"] += "-late"
    for component in a["fusion_result"]["components"]:
        component["anchor_scan_id"] += "-late"
    r = await send(client, a)
    assert r.status_code == 201, r.text
    assert row.payload == before
    page = await lidar_profiles(db, monitoring_source("pit-demo"), cloud_context["now"])
    assert all(p.average == pytest.approx(50.0125) for p in page.profiles)


async def test_flag_off_keeps_raw_graph_and_creates_no_filtered_window(
    client, test_db_session, world_context
):
    db = test_db_session
    await prepare(client, db)
    _, page = await frame(client, db, world_context, 0, 90)
    assert all(p.sampleSource == "MEASUREMENT" for p in page.profiles)
    assert page.profiles[0].average == pytest.approx(20, abs=0.05)
    assert await db.get(EdgeV2Latest, ("site-demo", "edge-demo", "filtered_profile_window")) is None


def test_angle_order_is_preserved_with_original_index_correspondence(world_context):
    def change(c, a, r, s):
        a["filtered_point_clouds"][0]["angles_mdeg"] = [20000, 10000]

    p = calculate(change)
    assert [s["sourceIndex"] for s in p.samples] == [7, 2]
    assert [s["positionRatio"] for s in p.samples] == pytest.approx([0.35, 0.25])
