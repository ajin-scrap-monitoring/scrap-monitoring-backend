"""Independent validation of pinned section geometry, clocks and unchanged LIMITED state."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.app.core.config import Settings
from src.app.schemas.edge_v2 import Assessment, Configuration, DeviceStatus, StatusReport
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.services.section_profile_context import SectionAnchor, project_section
from tests.test_edge_section_results import judgment, section, section_assessment
from tests.test_edge_world_height import REFERENCE, world_inputs
from tests.test_height_fill_pipeline import START
from tests.test_monitoring_v2 import DEVICE, SOURCE, status
from tests.test_section_profile_projection import profile


def inputs():
    c, _ = world_inputs()
    c["judgment"] = judgment()
    a = section_assessment()
    a.update(
        generated_at=START.isoformat(),
        observed_from=START.isoformat(),
        observed_to=START.isoformat(),
    )
    a["section_profiles"] = []
    for sid, height in [("lidar-a", 25), ("lidar-b", 75)]:
        value = profile((height, height)).model_dump()
        value.update(plane_origin_mm=[200, 200, 1000], plane_normal=[0.8, 0, 0.6])
        value["sensor_stats"][0]["sensor_id"] = sid
        for b in value["bins"]:
            b["sensor_ids"] = [sid]
        s = section(sid, height)
        s["profile"] = value
        a["section_profiles"].append(s)
        configured = next(s for s in c["judgment"]["sections"] if s["sensor_id"] == sid)
        configured.update(
            origin_xy_mm=value["origin_xy_mm"], axis_xy=value["axis_xy"], bin_width_mm=1000
        )
        component = next(x for x in a["fusion_result"]["components"] if x["sensor_id"] == sid)
        component.update(section_ratio=height / 100, calibrated_ratio=height / 100)
        for evidence in a["evidence"]["scans"]:
            if evidence["sensor_id"] == sid:
                evidence["calibration_version"] = "edge-cal-" + sid[-1]
    a["fusion_result"]["ratio"] = a["fill_estimate"]["ratio"] = 0.5
    raw_status = status(when=START)
    raw_status["config_revision"] = "cfg-1"
    return (
        Assessment.model_validate(a),
        Configuration.model_validate(c),
        SimulationHeightReference.model_validate(REFERENCE),
        SectionAnchor.model_validate(
            {
                "sensor_id": "lidar-a",
                "scan_id": "scan-a-1",
                "stream_instance_id": "synthetic-stream",
                "sequence": 1,
                "acquired_at": START.isoformat(),
                "clock_state": "UNSYNCED",
                "clock_domain_id": "synthetic-boot",
                "acquired_monotonic_ns": "1000000000",
            }
        ),
        DeviceStatus(
            site_id="site-demo",
            edge_id="edge-demo",
            connection_state="REPORTING",
            last_received_at=START,
            effective_state="HEALTHY",
            reason_codes=[],
            snapshot=StatusReport.model_validate(raw_status),
        ),
    )


def calculate(change=None):
    a, c, r, anchor, status_result = inputs()
    if change:
        change(a, c, r, anchor, status_result)
    return project_section(
        "lidar-a", a.section_profiles[0], a, c, r, anchor, status_result, START, 30
    )


def test_default_settings_curve_never_upgrades_its_wire_state():
    result = calculate()
    assert len(result.curve.samples) == 2
    assert result.wire_state == "LIMITED"
    assert result.area_ratio == 0.25
    assert "DEFAULT_SETTINGS" in result.reason_codes


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("revision", "SECTION_REFERENCE_MISMATCH"),
        ("frame", "SECTION_REFERENCE_MISMATCH"),
        ("calibration", "SECTION_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("plane", "SECTION_CALIBRATION_UNVERIFIED_OR_MISMATCHED"),
        ("geometry", "SECTION_PROFILE_SETTINGS_MISMATCH"),
        ("axis_plane", "SECTION_PROFILE_SETTINGS_MISMATCH"),
        ("anchor", "SECTION_ANCHOR_REFERENCE_MISMATCH"),
        ("anchor_sensor", "SECTION_ANCHOR_UNAVAILABLE"),
        ("old_anchor", "SECTION_PROFILE_STALE_OR_CLOCK_SKEW"),
        ("future_anchor", "SECTION_PROFILE_STALE_OR_CLOCK_SKEW"),
        ("old_generation", "SECTION_PROFILE_STALE_OR_CLOCK_SKEW"),
        ("future_generation", "SECTION_PROFILE_STALE_OR_CLOCK_SKEW"),
        ("sensor_missing", "SECTION_DEVICE_OR_SENSOR_UNAVAILABLE"),
        ("sensor_old", "SECTION_DEVICE_OR_SENSOR_UNAVAILABLE"),
        ("sensor_future", "SECTION_DEVICE_OR_SENSOR_UNAVAILABLE"),
        ("clock", "SECTION_DEVICE_OR_SENSOR_UNAVAILABLE"),
    ],
)
def test_inconsistent_or_expired_context_never_reuses_a_curve(kind, reason):
    def change(a, c, r, anchor, status_result):
        if kind == "revision":
            a.config_revision = "other-config"
        elif kind == "frame":
            r.coordinate_frame_id = "other-frame"
        elif kind == "calibration":
            r.calibration_versions["lidar-a"] = "other-calibration"
        elif kind == "plane":
            a.section_profiles[0].profile.plane_origin_mm[0] += 1
        elif kind == "geometry":
            a.section_profiles[0].profile.origin_xy_mm[0] += 1
        elif kind == "axis_plane":
            a.section_profiles[0].profile.axis_xy = [2**-0.5, 2**-0.5]
            c.judgment.sections[0].axis_xy = [2**-0.5, 2**-0.5]
        elif kind == "anchor":
            anchor.scan_id = "not-the-evidence"
        elif kind == "anchor_sensor":
            anchor.sensor_id = "lidar-b"
        elif kind == "old_anchor":
            anchor.acquired_at -= timedelta(seconds=1)
        elif kind == "future_anchor":
            anchor.acquired_at += timedelta(microseconds=1)
        elif kind == "old_generation":
            a.generated_at -= timedelta(seconds=30)
        elif kind == "future_generation":
            a.generated_at += timedelta(microseconds=1)
        elif kind == "sensor_missing":
            status_result.snapshot.sensors = []
        elif kind == "sensor_old":
            status_result.snapshot.sensors[0].last_scan_at = START - timedelta(seconds=30)
        elif kind == "sensor_future":
            status_result.snapshot.sensors[0].last_scan_at = START + timedelta(microseconds=1)
        else:
            anchor.clock_state = "DEGRADED"

    result = calculate(change)
    assert result.curve.samples == [] and result.area_ratio is None
    assert result.reason_codes == (reason,)


def test_expired_scan_marker_cannot_stand_in_for_anchor_metadata():
    with pytest.raises(ValidationError):
        SectionAnchor.model_validate({"raw_expired": True})


@pytest.mark.parametrize("kind", ["missing", "raw_range", "physical", "collection"])
def test_section_mode_requires_explicit_geometry_and_separate_collection_policy(kind):
    from tests.test_lidar_height import REFERENCE as PHYSICAL_REFERENCE

    settings = {
        "EDGE_V2_DEVICES": [DEVICE],
        "EDGE_V2_MONITORING_SOURCES": [SOURCE],
        "EDGE_V2_COMPACT_STORAGE_ENABLED": True,
        "EDGE_V2_HEIGHT_REFERENCES": [],
        "EDGE_V2_SIMULATION_HEIGHT_REFERENCES": [REFERENCE],
        "EDGE_V2_SECTION_PROFILES_ENABLED": True,
    }
    if kind == "missing":
        settings["EDGE_V2_SIMULATION_HEIGHT_REFERENCES"] = []
    elif kind == "raw_range":
        settings["EDGE_V2_SIMULATION_HEIGHT_REFERENCES"] = [
            {**REFERENCE, "coordinate_source": "RAW_RANGE", "calibration_versions": {}}
        ]
    elif kind == "physical":
        settings["EDGE_V2_SIMULATION_HEIGHT_REFERENCES"] = []
        settings["EDGE_V2_HEIGHT_REFERENCES"] = [PHYSICAL_REFERENCE]
    else:
        settings.update(
            EDGE_V2_OPERATIONS_ENABLED=True,
            EDGE_V2_HEIGHT_FILL_ENABLED=True,
            EDGE_V2_COLLECTION_DETECTION_ENABLED=True,
            EDGE_V2_COLLECTION_DROP_METERS=3,
        )
    with pytest.raises(ValidationError, match="Section profiles require"):
        Settings(_env_file=None, **settings)


def test_legacy_profile_serialization_omits_new_optional_section_fields():
    from src.app.schemas.monitoring_v2 import LidarProfilePageV2, LidarProfileV2

    profile = LidarProfileV2(sensorId="lidar-a", referenceDistanceCm=100)
    page = LidarProfilePageV2(pitId="pit-demo", profiles=[profile]).model_dump(mode="json")
    assert page["profiles"][0]["sampleSource"] == "MEASUREMENT"
    assert "sectionSource" not in page["profiles"][0]
    assert "sectionJudgmentState" not in page["profiles"][0]
    assert "rawMeanSectionFillPercent" not in page
