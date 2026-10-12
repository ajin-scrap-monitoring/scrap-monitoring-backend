"""Atomic v2 storage. No sensor values are converted into legacy fill estimates."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.edge_v2_security import EdgeV2Device, EdgeV2Error
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.schemas.edge_v2 import (
    Assessment,
    Configuration,
    DeviceStatus,
    Envelope,
    Measurement,
    StatusReport,
    StorageAck,
)


def utc(value: datetime) -> datetime:
    """SQLite drops timezone metadata; stored UTC values keep their meaning."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def message_query(payload: Envelope) -> Select[tuple[EdgeV2Message]]:
    return select(EdgeV2Message).where(
        EdgeV2Message.site_id == payload.site_id,
        EdgeV2Message.edge_id == payload.edge_id,
        EdgeV2Message.message_id == payload.message_id,
    )


async def store_artifacts(db: AsyncSession, payload: Envelope) -> None:
    artifacts: dict[tuple[str, str], tuple[list[Any], dict[str, Any], str]] = {}

    def add(kind: str, key: list[Any], content: dict[str, Any], field: str) -> None:
        identity = (kind, digest(key))
        item = (key, content, field)
        if identity in artifacts and artifacts[identity] != item:
            raise EdgeV2Error(409, "ARTIFACT_CONFLICT", field)
        artifacts[identity] = item

    if isinstance(payload, Configuration):
        content = payload.model_dump(mode="json", include={"coordinate_frame", "sensors"})
        add("configuration", [payload.config_revision], content, "config_revision")
        for configured_sensor in payload.sensors:
            if configured_sensor.calibration:
                add(
                    "calibration",
                    [configured_sensor.sensor_id, configured_sensor.calibration.version],
                    configured_sensor.calibration.model_dump(mode="json"),
                    "sensors",
                )
    elif isinstance(payload, Measurement):
        for sensor in payload.sensors:
            if sensor.scan is None:
                continue
            scan = sensor.scan
            raw = scan.model_dump(mode="json", exclude={"points", "selected_point_count"})
            raw["sensor_id"] = sensor.sensor_id
            add("scan_source", [scan.scan_id], raw, "sensors")
            # Keep points together as JSON, avoiding thousands of SQL rows per second.
            # A different config can select a different subset of the same source scan.
            add(
                "scan_result",
                [
                    scan.scan_id,
                    payload.config_revision,
                    sensor.calibration_version,
                    payload.coordinate_frame_id,
                ],
                sensor.model_dump(mode="json", exclude={"availability"}),
                "sensors",
            )
    elif isinstance(payload, Assessment):
        content = payload.model_dump(mode="json", exclude={"message_id"})
        add(
            "assessment_revision",
            [payload.assessment_id, payload.revision],
            content,
            "revision",
        )
    elif isinstance(payload, StatusReport):
        # Same instance/sequence is immutable, even when retransmitted under another message ID.
        content = payload.model_dump(mode="json", exclude={"message_id"})
        add(
            "status_sequence",
            [payload.orchestrator_instance_id, payload.sequence],
            content,
            "sequence",
        )

    # Batch lookups and inserts: a full scan must not perform one round trip per point.
    keys = list(artifacts)
    for attempt in range(3):
        found: dict[tuple[str, str], EdgeV2Artifact] = {}
        for start in range(0, len(keys), 400):
            rows = await db.scalars(
                select(EdgeV2Artifact).where(
                    EdgeV2Artifact.site_id == payload.site_id,
                    EdgeV2Artifact.edge_id == payload.edge_id,
                    tuple_(EdgeV2Artifact.kind, EdgeV2Artifact.key_hash).in_(
                        keys[start : start + 400]
                    ),
                )
            )
            found.update(((row.kind, row.key_hash), row) for row in rows)
        missing: list[EdgeV2Artifact] = []
        for identity, (key, content, field) in artifacts.items():
            if identity in found:
                existing = found[identity]
                if existing.artifact_key != key or existing.payload != content:
                    raise EdgeV2Error(409, "ARTIFACT_CONFLICT", field)
            else:
                missing.append(
                    EdgeV2Artifact(
                        site_id=payload.site_id,
                        edge_id=payload.edge_id,
                        kind=identity[0],
                        key_hash=identity[1],
                        artifact_key=key,
                        payload=content,
                    )
                )
        if not missing:
            return
        try:
            # The unique constraint arbitrates concurrent writers across processes.
            async with db.begin_nested():
                db.add_all(missing)
                await db.flush()
            return
        except IntegrityError:
            if attempt == 2:
                raise EdgeV2Error(503, "STORAGE_UNAVAILABLE", retryable=True) from None


def validate_scope(payload: Envelope, device: EdgeV2Device) -> None:
    if (payload.site_id, payload.edge_id) != (device.site_id, device.edge_id):
        raise EdgeV2Error(403, "DEVICE_SCOPE_MISMATCH")
    if isinstance(payload, (Measurement, Configuration, StatusReport)):
        sensor_ids = {sensor.sensor_id for sensor in payload.sensors}
        expected = set(device.sensor_ids)
        if not sensor_ids <= expected:
            raise EdgeV2Error(422, "SCHEMA_VALIDATION_FAILED", "sensors")
        if isinstance(payload, Configuration) or (
            isinstance(payload, Measurement) and payload.delivery_kind == "PERIODIC"
        ):
            if sensor_ids != expected:
                raise EdgeV2Error(422, "SCHEMA_VALIDATION_FAILED", "sensors")


def receipt(message: EdgeV2Message, duplicate: bool) -> StorageAck:
    return StorageAck(
        message_id=message.message_id, duplicate=duplicate, received_at=utc(message.received_at)
    )


async def accept_message(
    db: AsyncSession, payload: Envelope, original: dict[str, Any], device: EdgeV2Device
) -> StorageAck:
    """Commit all artifacts before issuing the ACK; replay returns the original receipt."""
    validate_scope(payload, device)
    body_hash = digest(original)
    try:
        existing = await db.scalar(message_query(payload))
        if existing is not None:
            if existing.body_hash != body_hash:
                raise EdgeV2Error(409, "IDEMPOTENCY_CONFLICT")
            return receipt(existing, True)
        message = EdgeV2Message(
            site_id=payload.site_id,
            edge_id=payload.edge_id,
            message_id=payload.message_id,
            message_type=str(original["message_type"]),
            config_revision=payload.config_revision,
            generated_at=payload.generated_at,
            received_at=datetime.now(UTC),
            body_hash=body_hash,
            payload=original,
            instance_id=payload.orchestrator_instance_id
            if isinstance(payload, StatusReport)
            else None,
            sequence=payload.sequence if isinstance(payload, StatusReport) else None,
        )
        db.add(message)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(message_query(payload))
            if existing is None:
                raise EdgeV2Error(503, "STORAGE_UNAVAILABLE", retryable=True) from None
            if existing.body_hash != body_hash:
                raise EdgeV2Error(409, "IDEMPOTENCY_CONFLICT") from None
            return receipt(existing, True)
        await store_artifacts(db, payload)
        await db.commit()
        return receipt(message, False)
    except EdgeV2Error:
        await db.rollback()
        raise
    except SQLAlchemyError:
        await db.rollback()
        raise EdgeV2Error(503, "STORAGE_UNAVAILABLE", retryable=True) from None


async def device_status(
    db: AsyncSession, site_id: str, edge_id: str, now: datetime | None = None
) -> DeviceStatus:
    """Receipt time controls connectivity; sequence controls snapshot within an instance."""
    query = select(EdgeV2Message).where(
        EdgeV2Message.site_id == site_id,
        EdgeV2Message.edge_id == edge_id,
        EdgeV2Message.message_type == "edge_status",
    )
    latest_instance = await db.scalar(
        query.order_by(EdgeV2Message.generated_at.desc(), EdgeV2Message.id.desc()).limit(1)
    )
    last_receipt = await db.scalar(query.order_by(EdgeV2Message.received_at.desc()).limit(1))
    snapshot: StatusReport | None = None
    if latest_instance:
        latest = await db.scalar(
            query.where(EdgeV2Message.instance_id == latest_instance.instance_id)
            .order_by(EdgeV2Message.sequence.desc(), EdgeV2Message.id.desc())
            .limit(1)
        )
        if latest:
            snapshot = StatusReport.model_validate(latest.payload)
    received_at = utc(last_receipt.received_at) if last_receipt else None
    reporting = received_at is not None and (
        (now or datetime.now(UTC)) - received_at < timedelta(seconds=30)
    )
    return DeviceStatus(
        site_id=site_id,
        edge_id=edge_id,
        connection_state="REPORTING" if reporting else "UNKNOWN",
        last_received_at=received_at,
        effective_state=snapshot.overall_state if reporting and snapshot else "UNKNOWN",
        reason_codes=snapshot.reason_codes if reporting and snapshot else ["STATUS_NOT_RECEIVED"],
        snapshot=snapshot,
    )
