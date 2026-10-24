"""Transactional, bounded observations; immutable receipt/artifact hashes remain separate."""

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2 import EdgeV2Message
from src.app.models.edge_v2_projection import (
    EdgeV2Filter,
    EdgeV2Latest,
    EdgeV2Minute,
    EdgeV2Transition,
)
from src.app.schemas.edge_v2 import Assessment, Envelope, Measurement, StatusReport
from src.app.schemas.fill_observation import FillObservation
from src.app.services.edge_v2_service import utc
from src.app.services.observation_window import observation_slots


async def lock_projection(db: AsyncSession, payload: Envelope) -> EdgeV2Filter:
    """Create a per-device lock with conflict-safe insertion, including concurrent first use."""
    statement = (
        postgresql.insert(EdgeV2Filter)
        if db.get_bind().dialect.name == "postgresql"
        else sqlite.insert(EdgeV2Filter)
    )
    await db.execute(
        statement.values(
            site_id=payload.site_id, edge_id=payload.edge_id, payload={}
        ).on_conflict_do_nothing(index_elements=["site_id", "edge_id"])
    )
    row = await db.scalar(
        select(EdgeV2Filter)
        .where(EdgeV2Filter.site_id == payload.site_id, EdgeV2Filter.edge_id == payload.edge_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


def receipt_payload(payload: Envelope) -> dict[str, Any]:
    """Do not retain another copy of scan points or every periodic STATE/status body."""
    if isinstance(payload, Assessment) and payload.assessment_type == "EVENT":
        return payload.model_dump(mode="json")
    if not isinstance(payload, (Measurement, Assessment, StatusReport)):
        return payload.model_dump(mode="json")
    return {
        **payload.model_dump(mode="json", include=set(Envelope.model_fields)),
        "message_type": payload.message_type,
        "assessment_type": payload.assessment_type if isinstance(payload, Assessment) else None,
        "retention": "LATEST_ONLY",
    }


async def latest_message(
    db: AsyncSession, site_id: str, edge_id: str, kind: str
) -> EdgeV2Message | None:
    """Return a detached view of a retained body without replacing a receipt's compact JSON."""
    latest = await db.get(EdgeV2Latest, (site_id, edge_id, kind))
    if latest is None:
        return None
    receipt = await db.scalar(
        select(EdgeV2Message).where(
            EdgeV2Message.site_id == site_id,
            EdgeV2Message.edge_id == edge_id,
            EdgeV2Message.message_id == latest.message_id,
        )
    )
    if receipt is None:
        return None
    return EdgeV2Message(
        id=receipt.id,
        site_id=site_id,
        edge_id=edge_id,
        message_id=receipt.message_id,
        message_type=receipt.message_type,
        generated_at=receipt.generated_at,
        received_at=receipt.received_at,
        payload=latest.payload,
        compact=True,
        config_revision=receipt.config_revision,
        body_hash=receipt.body_hash,
        instance_id=receipt.instance_id,
        sequence=receipt.sequence,
    )


async def project(
    db: AsyncSession,
    payload: Envelope,
    original: dict[str, Any],
    receipt: EdgeV2Message,
    filter_row: EdgeV2Filter,
) -> None:
    kind: str
    if isinstance(payload, Measurement):
        if payload.delivery_kind != "PERIODIC":
            return  # Historical evidence never replaces a live observation.
        kind = "measurement"
    elif isinstance(payload, Assessment):
        if payload.assessment_type != "STATE":
            return
        kind = "state"
    elif isinstance(payload, StatusReport):
        kind = "status"
    else:
        return
    current = await db.get(EdgeV2Latest, (payload.site_id, payload.edge_id, kind))
    replace = current is None or payload.generated_at > utc(current.generated_at)
    same_instance = False
    old_status = None
    if current is not None and isinstance(payload, StatusReport):
        previous = StatusReport.model_validate(current.payload)
        old_status = previous.overall_state
        same_instance = previous.orchestrator_instance_id == payload.orchestrator_instance_id
        if same_instance:
            replace = payload.sequence > previous.sequence
            # Instance selection uses generation time; its snapshot uses sequence.
            if payload.generated_at > utc(current.generated_at):
                current.generated_at = payload.generated_at
    if not replace:
        return
    if current is None:
        current = EdgeV2Latest(
            site_id=payload.site_id,
            edge_id=payload.edge_id,
            kind=kind,
            message_id=payload.message_id,
            generated_at=payload.generated_at,
            payload=original,
        )
        db.add(current)
    else:
        current.message_id = payload.message_id
        current.generated_at = max(utc(current.generated_at), payload.generated_at)
        current.payload = original
    if isinstance(payload, StatusReport):
        from src.app.services.height_median_service import (
            height_median_enabled,
            invalidate_on_status,
        )

        if height_median_enabled():
            await invalidate_on_status(db, payload, utc(receipt.received_at), same_instance)
        if old_status is not None and old_status != payload.overall_state:
            db.add(
                EdgeV2Transition(
                    site_id=payload.site_id,
                    edge_id=payload.edge_id,
                    occurred_at=payload.generated_at,
                    kind="DEVICE_STATE_CHANGED",
                    payload={
                        "previous": old_status,
                        "current": payload.overall_state,
                        "messageId": payload.message_id,
                        "reasonCodes": payload.reason_codes,
                    },
                )
            )
        if not same_instance or payload.overall_state not in ("HEALTHY", "DEGRADED"):
            filter_row.payload = {}
        if get_settings().EDGE_V2_HEIGHT_FILL_ENABLED:
            from src.app.services.height_reference import device_clock_allowed

            observed_now = utc(receipt.received_at)
            active_scope = filter_row.payload.get("scope", [])
            if (
                not device_clock_allowed(payload.site_id, payload.edge_id, payload.clock.state)
                or payload.generated_at > observed_now
                or len(payload.sensors) != 2
                or any(
                    s.data_state != "FRESH"
                    or s.last_scan_at is None
                    or not timedelta(0)
                    <= observed_now - s.last_scan_at
                    < timedelta(seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
                    for s in payload.sensors
                )
                or active_scope
                and active_scope[0] != payload.config_revision
            ):
                filter_row.payload = {}
    elif isinstance(payload, Measurement) and (
        get_settings().EDGE_V2_HEIGHT_FILL_ENABLED or get_settings().EDGE_V2_HEIGHT_MEDIAN_ENABLED
    ):
        from src.app.services.height_fill_service import project_height_fill

        await db.flush()
        await project_height_fill(db, payload, receipt, filter_row)
    elif isinstance(payload, Assessment) and not get_settings().EDGE_V2_HEIGHT_FILL_ENABLED:
        await project_state(db, payload, receipt, filter_row)


async def project_state(
    db: AsyncSession, assessment: Assessment, receipt: EdgeV2Message, row: EdgeV2Filter
) -> None:
    from src.app.services.load_history_v2_service import historical_value

    value, state, reasons = historical_value(assessment, utc(receipt.received_at))
    estimate = assessment.fill_estimate
    observation = FillObservation(
        source="EDGE_STATE",
        site_id=assessment.site_id,
        edge_id=assessment.edge_id,
        config_revision=assessment.config_revision,
        message_id=assessment.message_id,
        observation_id=assessment.assessment_id,
        generated_at=assessment.generated_at,
        received_at=utc(receipt.received_at),
        observed_to=assessment.observed_to,
        rule_version=assessment.rule_version,
        method_version=estimate.method_version if estimate else None,
        value_percent=estimate.ratio * 100 if estimate and value is not None else value,
        data_state=state,
        reason_codes=reasons,
    )
    await project_input(db, observation, receipt, row)


async def project_input(
    db: AsyncSession,
    assessment: FillObservation,
    receipt: EdgeV2Message,
    row: EdgeV2Filter,
    *,
    height_filtered_percent: float | None = None,
) -> None:
    from src.app.services.edge_v2_service import device_status

    settings = get_settings()
    now = utc(receipt.received_at)
    value, state, reasons = assessment.value_percent, assessment.data_state, assessment.reason_codes
    status = await device_status(db, assessment.site_id, assessment.edge_id, now)
    observed = assessment.observed_to
    valid = (
        value is not None
        and observed is not None
        and observed <= now
        and (
            now - observed < timedelta(seconds=settings.EDGE_V2_ASSESSMENT_TTL_SECONDS)
            and status.connection_state == "REPORTING"
            and status.effective_state in ("HEALTHY", "DEGRADED")
            and status.snapshot is not None
            and status.snapshot.config_revision == assessment.config_revision
        )
    )
    assignment = next(
        (
            s
            for s in settings.EDGE_V2_MONITORING_SOURCES
            if (s.site_id, s.edge_id) == (assessment.site_id, assessment.edge_id)
        ),
        None,
    )
    pit = await db.get(PitConfig, assignment.pit_id) if assignment else None
    threshold = pit.critical_threshold_percent if pit else settings.EDGE_V2_CRITICAL_THRESHOLD
    scope = [
        assessment.config_revision,
        assessment.rule_version,
        assessment.method_version,
        status.snapshot.orchestrator_instance_id if status.snapshot else None,
        assignment.pit_id if assignment else None,
        threshold,
        assessment.source,
        assessment.scope_token,
    ]
    previous = row.payload
    if previous.get("inputSource") and (
        previous["inputSource"] != assessment.source
        or previous.get("methodVersion") != assessment.method_version
    ):
        db.add(
            EdgeV2Transition(
                site_id=assessment.site_id,
                edge_id=assessment.edge_id,
                occurred_at=assessment.generated_at,
                kind="FILL_INPUT_CHANGED",
                payload={
                    "inputSource": assessment.source,
                    "methodVersion": assessment.method_version,
                    "previousInputSource": previous["inputSource"],
                    "previousMethodVersion": previous.get("methodVersion"),
                    "messageId": assessment.message_id,
                    "observationId": assessment.observation_id,
                },
            )
        )
    samples = list(previous.get("samples", [])) if previous.get("scope") == scope else []
    anchor_text = previous.get("slot_anchor_at") if previous.get("scope") == scope else None
    anchor = datetime.fromisoformat(anchor_text) if anchor_text else None
    filtered: float | None = None
    filter_state = "UNAVAILABLE"
    if valid and observed is not None:
        timestamp = observed.timestamp()
        last_observed = previous.get("observed_at")
        if last_observed and observed <= datetime.fromisoformat(last_observed):
            # Increasing generated_at cannot turn an old observation into a new sample.
            valid = False
            samples = []
            anchor = None
        else:
            if last_observed and observed - datetime.fromisoformat(last_observed) > timedelta(
                seconds=1.5
            ):
                samples = []
                anchor = None
            second = int(timestamp)
            samples.append(
                {
                    "at": timestamp,
                    "second": second,
                    "value": value,
                }
            )
            selected, anchor = observation_slots(
                [datetime.fromtimestamp(sample["at"], UTC) for sample in samples],
                observed,
                settings.EDGE_V2_MEDIAN_WINDOW_SECONDS,
                anchor,
            )
            samples = [samples[index] for index in selected]
            filter_state = "WARMING"
            if len(samples) == settings.EDGE_V2_MEDIAN_WINDOW_SECONDS:
                # Height bins have already been filtered in cm. Derive the ratio once;
                # a second percentage median would add another smoothing delay.
                filtered = (
                    height_filtered_percent
                    if assessment.source == "HEIGHT_SECTIONS"
                    else float(median(s["value"] for s in samples))
                )
                filter_state = "READY" if filtered is not None else "WARMING"
    else:
        samples = []
        anchor = None
    above_since = None
    if filtered is not None and filtered >= threshold:
        if (
            previous.get("scope") == scope
            and previous.get("state") == "READY"
            and samples
            and len(samples) == settings.EDGE_V2_MEDIAN_WINDOW_SECONDS
        ):
            above_since = previous.get("above_since")
        above_since = above_since or (observed.isoformat() if observed else None)
    if previous.get("input_valid") is not None and previous["input_valid"] != valid:
        db.add(
            EdgeV2Transition(
                site_id=assessment.site_id,
                edge_id=assessment.edge_id,
                occurred_at=assessment.generated_at,
                kind="MEASUREMENT_VALIDITY_CHANGED",
                payload={
                    "previousValid": previous["input_valid"],
                    "currentValid": valid,
                    "assessmentId": assessment.assessment_id
                    if assessment.source == "EDGE_STATE"
                    else None,
                    "observationId": assessment.observation_id,
                    "inputSource": assessment.source,
                    "messageId": assessment.message_id,
                    "reasonCodes": assessment.reason_codes,
                },
            )
        )
    row.payload = {
        "scope": scope,
        "samples": samples,
        "slot_anchor_at": anchor.isoformat() if anchor else None,
        "state": filter_state,
        "message_id": assessment.message_id,
        "value": filtered,
        "observed_at": observed.isoformat() if observed else None,
        "above_since": above_since,
        "threshold": threshold,
        "input_valid": valid,
        "inputSource": assessment.source,
        "methodVersion": assessment.method_version,
    }
    reason = (
        reasons
        if filtered is not None
        else list(
            dict.fromkeys(
                [
                    *reasons,
                    "FILTER_WARMING" if valid else "FILTER_INPUT_UNAVAILABLE",
                ]
            )
        )
    )
    sample = {
        "generatedAt": assessment.generated_at.isoformat(),
        "observedAt": observed.isoformat() if observed else None,
        "assessmentId": assessment.assessment_id if assessment.source == "EDGE_STATE" else None,
        "observationId": assessment.observation_id,
        "inputSource": assessment.source,
        "methodVersion": assessment.method_version,
        "valuePercent": filtered,
        "valid": filtered is not None,
        "dataState": state if filtered is not None else "UNKNOWN",
        "reasonCodes": reason,
    }
    bucket = assessment.generated_at.replace(second=0, microsecond=0)
    minute = await db.get(EdgeV2Minute, (assessment.site_id, assessment.edge_id, bucket))
    if minute is None:
        minute = EdgeV2Minute(
            site_id=assessment.site_id,
            edge_id=assessment.edge_id,
            bucket_start=bucket,
            generated_at=assessment.generated_at,
            payload={},
        )
        db.add(minute)
    data = dict(minute.payload)
    # Sixty scalar slots allow same-second replacement without inflating counts or
    # retaining sixty STATE bodies. No scan arrays or historical profiles live here.
    second = int(assessment.generated_at.timestamp())
    values = dict(data.get("values", {}))
    values[str(second)] = filtered
    sources = dict(data.get("sources", {}))
    methods = dict(data.get("methods", {}))
    sources[str(second)] = assessment.source
    methods[str(second)] = assessment.method_version
    valid_values = [v for v in values.values() if v is not None]
    data.update(
        last=sample,
        values=values,
        sources=sources,
        methods=methods,
        valid_count=len(valid_values),
        invalid_count=len(values) - len(valid_values),
        sum=sum(valid_values),
        minimum=min(valid_values) if valid_values else None,
        maximum=max(valid_values) if valid_values else None,
    )
    minute.generated_at = assessment.generated_at
    minute.payload = data
