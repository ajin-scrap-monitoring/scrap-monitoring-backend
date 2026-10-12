"""Edge contract 2.0-r1: wire units stay mm/mdeg; no backend fill inference."""

import math
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Counter = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]
Integer = Annotated[int, Field(strict=True)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]


def require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return value


def require_timestamp(value: object) -> object:
    if not isinstance(value, (str, datetime)):
        raise ValueError("RFC3339 timestamp required")
    if isinstance(value, str) and not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value, re.IGNORECASE
    ):
        raise ValueError("UTC RFC3339 timestamp required")
    return value


UtcTime = Annotated[AwareDatetime, BeforeValidator(require_timestamp), AfterValidator(require_utc)]
Capability = Literal["NOT_IMPLEMENTED", "NOT_VALIDATED", "ENABLED"]
TransformState = Literal["VERIFIED", "UNVERIFIED", "UNAVAILABLE"]


def require_integer(value: object) -> object:
    if value is not None and type(value) is not int:
        raise ValueError("Integer required")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Envelope(ContractModel):
    schema_version: Literal["2.0"]
    message_id: Identifier
    site_id: Identifier
    edge_id: Identifier
    generated_at: UtcTime
    config_revision: Identifier


class AngleInterval(ContractModel):
    start_inclusive: Annotated[int, Field(strict=True, ge=0, lt=360000)]
    end_exclusive: Annotated[int, Field(strict=True, gt=0, le=360000)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start_inclusive >= self.end_exclusive:
            raise ValueError("Angle interval must have positive width")
        return self


def validate_intervals(intervals: list[AngleInterval]) -> None:
    ordered = sorted(intervals, key=lambda item: item.start_inclusive)
    if any(a.end_exclusive > b.start_inclusive for a, b in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("Angle intervals must not overlap")


class Position(ContractModel):
    x: Integer
    y: Integer
    z: Integer


class Point(ContractModel):
    index: Counter
    angle_mdeg: Annotated[int, Field(strict=True, ge=0, lt=360000)]
    # Invalid SDK ranges are retained too; null coordinates carry their interpretation.
    distance_mm: Integer
    quality: Integer
    position_mm: Position | None
    calculation_reason_codes: list[Identifier]


class Scan(ContractModel):
    scan_id: Identifier
    stream_instance_id: Identifier
    sequence: Counter
    acquired_at: UtcTime
    timestamp_source: Identifier
    clock_domain_id: Identifier
    acquired_monotonic_ns: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$")]
    clock_state: Identifier
    clock_offset_ms: Number | None
    source_point_count: Counter
    selected_point_count: Counter
    points: list[Point]

    @model_validator(mode="after")
    def point_counts(self) -> Self:
        if self.selected_point_count != len(self.points):
            raise ValueError("selected_point_count must match points length")
        if self.selected_point_count > self.source_point_count:
            raise ValueError("Selected points exceed source points")
        indices = [point.index for point in self.points]
        if len(indices) != len(set(indices)) or any(
            index >= self.source_point_count for index in indices
        ):
            raise ValueError("Point indices must be unique source indices")
        return self


class SensorObservation(ContractModel):
    sensor_id: Identifier
    availability: Literal["PRESENT", "NO_NEW_SCAN", "STALE", "REJECTED", "CONFIG_UNAVAILABLE"]
    calibration_version: Identifier | None
    transform_state: TransformState
    transform_reason_codes: list[Identifier]
    angle_intervals_mdeg: list[AngleInterval]
    scan: Scan | None

    @model_validator(mode="after")
    def observation_consistency(self) -> Self:
        validate_intervals(self.angle_intervals_mdeg)
        if (self.availability == "PRESENT") != (self.scan is not None):
            raise ValueError("Only PRESENT contains a scan")
        if self.scan:
            if self.transform_state == "VERIFIED" and self.calibration_version is None:
                raise ValueError("Verified transform requires calibration version")
            for point in self.scan.points:
                if self.transform_state != "VERIFIED" and point.position_mm is not None:
                    raise ValueError("Unverified transform requires null coordinates")
                if not any(
                    interval.start_inclusive <= point.angle_mdeg < interval.end_exclusive
                    for interval in self.angle_intervals_mdeg
                ):
                    raise ValueError("Point is outside selected angle intervals")
        return self


class Measurement(Envelope):
    message_type: Literal["lidar_measurement"]
    delivery_kind: Literal["PERIODIC", "EVIDENCE"]
    report_interval_ms: Annotated[Literal[1000] | None, BeforeValidator(require_integer)]
    selection_policy: Literal["LATEST_TIME_MATCHED_PAIR", "REFERENCED_SCANS"]
    coordinate_frame_id: Identifier
    pair_state: Literal["MATCHED", "UNMATCHED", "PARTIAL", "NONE", "NOT_APPLICABLE"]
    pair_delta_ms: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] | None
    sensors: list[SensorObservation] = Field(min_length=1, max_length=2)
    assessment_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def measurement_consistency(self) -> Self:
        if len({sensor.sensor_id for sensor in self.sensors}) != len(self.sensors):
            raise ValueError("Sensor IDs must be unique")
        scans = [sensor.scan for sensor in self.sensors if sensor.scan is not None]
        if self.delivery_kind == "EVIDENCE":
            if (
                self.report_interval_ms is not None
                or self.selection_policy != "REFERENCED_SCANS"
                or self.pair_state != "NOT_APPLICABLE"
                or self.pair_delta_ms is not None
                or len(scans) != len(self.sensors)
                or not self.assessment_ids
            ):
                raise ValueError("Evidence requires referenced scans and assessment IDs")
            return self
        if (
            len(self.sensors) != 2
            or self.report_interval_ms != 1000
            or self.selection_policy != "LATEST_TIME_MATCHED_PAIR"
            or self.assessment_ids
        ):
            raise ValueError("Periodic measurements require both sensor slots")
        expected = {0: {"NONE"}, 1: {"PARTIAL"}, 2: {"MATCHED", "UNMATCHED"}}
        if self.pair_state not in expected[len(scans)]:
            raise ValueError("pair_state does not match available scans")
        for scan in scans:
            if not 0 <= (self.generated_at - scan.acquired_at).total_seconds() <= 1:
                raise ValueError("Periodic scan must be at most one second old at generation")
        if len(scans) < 2:
            if self.pair_delta_ms is not None:
                raise ValueError("Incomplete pair requires null pair_delta_ms")
        else:
            first, second = scans
            comparable = first.clock_domain_id == second.clock_domain_id
            if comparable:
                try:
                    delta = (
                        abs(int(first.acquired_monotonic_ns) - int(second.acquired_monotonic_ns))
                        / 1_000_000
                    )
                except (ValueError, OverflowError):
                    raise ValueError("Monotonic scan time cannot be compared") from None
                if self.pair_delta_ms is None or not math.isclose(
                    self.pair_delta_ms, delta, abs_tol=0.001
                ):
                    raise ValueError("pair_delta_ms must match monotonic scan times")
                if (self.pair_state == "MATCHED") != (delta <= 100):
                    raise ValueError("MATCHED requires a comparable pair within 100 ms")
            elif self.pair_delta_ms is not None or self.pair_state == "MATCHED":
                raise ValueError("Different clock domains cannot form a matched pair")
        return self


class Capabilities(ContractModel):
    fill: Capability
    occlusion: Capability
    collection: Capability
    vision: Capability


class FillEstimate(ContractModel):
    ratio: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
    method_version: Identifier


class Event(ContractModel):
    kind: Literal["FILL_INCREASE", "FILL_DECREASE", "OCCLUSION", "COLLECTION", "GRAPPLE_PRESENT"]
    stage: Literal["SUSPECTED", "CONFIRMED", "RESOLVED", "RETRACTED"]


class ScanReference(ContractModel):
    scan_id: Identifier
    sensor_id: Identifier
    config_revision: Identifier
    calibration_version: Identifier | None
    coordinate_frame_id: Identifier


class VisionReference(ContractModel):
    camera_id: Identifier
    stream_instance_id: Identifier
    frame_sequence: Counter
    frame_acquired_at: UtcTime
    timestamp_source: Identifier
    observation_id: Identifier
    model_version: Identifier


class Evidence(ContractModel):
    scans: list[ScanReference]
    vision_observations: list[VisionReference]


class Assessment(Envelope):
    message_type: Literal["edge_assessment"]
    assessment_type: Literal["STATE", "EVENT"]
    assessment_id: Identifier
    revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
    observed_from: UtcTime | None
    observed_to: UtcTime | None
    rule_version: Identifier | None
    decision_state: Literal["AVAILABLE", "LIMITED", "UNAVAILABLE"]
    reason_codes: list[Identifier]
    # Additional fill-state values are not enumerated in 2.0-r1.
    fill_state: Identifier
    fill_estimate: FillEstimate | None
    event: Event | None
    capabilities: Capabilities
    evidence: Evidence

    @model_validator(mode="after")
    def assessment_consistency(self) -> Self:
        if (self.observed_from is None) != (self.observed_to is None):
            raise ValueError("Observation times must both be null or present")
        if self.observed_from and self.observed_to and self.observed_from > self.observed_to:
            raise ValueError("Observation time range must be ordered")
        if self.assessment_type == "STATE":
            if self.revision != 1 or self.event is not None:
                raise ValueError("STATE requires revision 1 and null event")
        elif self.event is None:
            raise ValueError("EVENT requires kind and stage")
        if self.fill_estimate is not None:
            if (
                self.capabilities.fill != "ENABLED"
                or self.fill_state == "UNAVAILABLE"
                or self.decision_state == "UNAVAILABLE"
                or self.rule_version is None
            ):
                raise ValueError("Fill estimate requires validated enabled capability and rule")
        elif self.capabilities.fill != "ENABLED" and self.fill_state != "UNAVAILABLE":
            raise ValueError("Unvalidated fill requires UNAVAILABLE and null estimate")
        return self


class Clock(ContractModel):
    state: Identifier
    offset_ms: Number | None


class SensorStatus(ContractModel):
    sensor_id: Identifier
    data_state: Literal["FRESH", "STALE", "RECOVERING", "UNAVAILABLE", "UNKNOWN"]
    last_scan_at: UtcTime | None
    reason_codes: list[Identifier]


class ServiceStatus(ContractModel):
    service: Identifier
    state: Literal["STARTING", "HEALTHY", "DEGRADED", "RETRYING", "FATAL", "UNKNOWN"]
    last_progress_at: UtcTime | None
    reason_codes: list[Identifier]


class DeliveryStatus(ContractModel):
    pending_messages: Counter
    pending_bytes: Counter
    quarantined_messages: Counter
    last_backend_ack_at: UtcTime | None


class StorageStatus(ContractModel):
    limit_bytes: Counter
    used_bytes: Counter
    lost_messages: Counter
    loss_from: UtcTime | None
    loss_to: UtcTime | None
    last_loss_reason: Identifier | None
    unavailable_evidence_scan_ids: list[Identifier]

    @model_validator(mode="after")
    def ordered_loss(self) -> Self:
        if (self.loss_from is None) != (self.loss_to is None):
            raise ValueError("Loss times must both be null or present")
        if self.loss_from and self.loss_to and self.loss_from > self.loss_to:
            raise ValueError("Loss time range must be ordered")
        return self


class StatusReport(Envelope):
    message_type: Literal["edge_status"]
    orchestrator_instance_id: Identifier
    sequence: Counter
    overall_state: Literal[
        "HEALTHY", "DEGRADED", "MEASUREMENT_UNAVAILABLE", "CONFIG_ERROR", "UNKNOWN"
    ]
    reason_codes: list[Identifier]
    clock: Clock
    sensors: list[SensorStatus]
    services: list[ServiceStatus]
    delivery: DeliveryStatus
    storage: StorageStatus

    @model_validator(mode="after")
    def distinct_components(self) -> Self:
        if len({s.sensor_id for s in self.sensors}) != len(self.sensors):
            raise ValueError("Sensor IDs must be unique")
        if len({s.service for s in self.services}) != len(self.services):
            raise ValueError("Service names must be unique")
        return self


class CoordinateFrame(ContractModel):
    frame_id: Identifier
    unit: Literal["mm"]
    origin_description: Annotated[str, Field(min_length=1)]
    x_axis_description: Annotated[str, Field(min_length=1)]
    y_axis_description: Annotated[str, Field(min_length=1)]
    z_axis_description: Annotated[str, Field(min_length=1)]
    handedness: Literal["RIGHT_HANDED"]


Vector = tuple[Number, Number, Number]


class Calibration(ContractModel):
    version: Identifier
    verification_state: Literal["VERIFIED", "UNVERIFIED"]
    angle_sign: Annotated[Literal[-1, 1], BeforeValidator(require_integer)]
    angle_zero_offset_mdeg: Integer
    rotation: tuple[Vector, Vector, Vector]
    translation_mm: Vector

    @model_validator(mode="after")
    def proper_rotation(self) -> Self:
        r = self.rotation
        for i in range(3):
            for j in range(3):
                dot = sum(r[i][k] * r[j][k] for k in range(3))
                if not math.isclose(dot, float(i == j), abs_tol=1e-6):
                    raise ValueError("Rotation must be orthonormal")
        determinant = (
            r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
            - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
            + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
        )
        if not math.isclose(determinant, 1.0, abs_tol=1e-6):
            raise ValueError("Rotation must preserve handedness")
        return self


class SensorConfiguration(ContractModel):
    sensor_id: Identifier
    angle_intervals_mdeg: list[AngleInterval] = Field(min_length=1)
    calibration: Calibration | None

    @model_validator(mode="after")
    def valid_angles(self) -> Self:
        validate_intervals(self.angle_intervals_mdeg)
        return self


class Configuration(Envelope):
    message_type: Literal["edge_configuration"]
    coordinate_frame: CoordinateFrame
    sensors: list[SensorConfiguration] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def distinct_sensors(self) -> Self:
        if len({sensor.sensor_id for sensor in self.sensors}) != len(self.sensors):
            raise ValueError("Sensor IDs must be unique")
        return self


class StorageAck(ContractModel):
    message_id: Identifier
    accepted: Literal[True] = True
    duplicate: bool
    received_at: UtcTime


class ErrorDetail(ContractModel):
    code: Identifier
    retryable: bool
    field: str | None


class StorageError(ContractModel):
    message_id: Identifier | None
    accepted: Literal[False] = False
    error: ErrorDetail


class DeviceStatus(ContractModel):
    site_id: Identifier
    edge_id: Identifier
    connection_state: Literal["REPORTING", "UNKNOWN"]
    last_received_at: UtcTime | None
    effective_state: str
    reason_codes: list[str]
    snapshot: StatusReport | None
