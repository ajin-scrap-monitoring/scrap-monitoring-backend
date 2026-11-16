"""Bounded operator episodes; raw validity facts and live calculation stay independent."""

import hashlib
import json
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.edge_v2 import UtcTime
from src.app.schemas.fill_observation import FillObservation

RECOVERY_SECONDS = 10
MAX_GAP = timedelta(seconds=1.5)
MAX_CAUSES = 128
PROVENANCE = {
    "SIMULATION_GEOMETRY",
    "SIMULATION_SOURCE_CLOCK_UNVERIFIED",
    "HEIGHT_SECTION_ESTIMATE",
    "EDGE_WORLD_COORDINATES",
    "EDGE_SECTION_FUSION_INPUT",
}


class Episode(BaseModel):
    version: Literal[1] = 1
    scope: str
    method: str | None = None
    baseline: StrictBool = False
    causes: list[str] = Field(default_factory=list, max_length=MAX_CAUSES)
    observed_since: UtcTime | None = None
    received_since: UtcTime | None = None
    last_observed: UtcTime | None = None
    last_generated: UtcTime | None = None
    last_received: UtcTime | None = None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def mark_measurement_episode(
    db: AsyncSession,
    observation: FillObservation,
    valid: bool,
    scope: list[Any],
    previous_valid: bool | None,
    pit_id: str | None,
    transition: EdgeV2Transition | None,
) -> None:
    """Called under the device projection lock and the original receipt transaction.

    Only new valid observations can confirm recovery. Both observation time and
    receipt time must span ten continuous seconds, so replayed/backlogged data and
    GET requests cannot recover an episode. Nothing changes current health, input
    validity, the median, or a previously committed transition.
    """
    key = (observation.site_id, observation.edge_id, "measurement_error_episodes")
    row = await db.get(EdgeV2Latest, key)
    # Unavailable reports can omit their fill estimate/method. Absence is not a
    # new calculation context; an explicit new method still starts a new episode.
    context = list(scope)
    method = context[2] if len(context) > 2 else None
    if len(context) > 2:
        context[2] = None
    scope_hash = digest(context)
    try:
        state = Episode.model_validate(row.payload) if row is not None else None
    except ValidationError:
        state = None
    if (
        state is None
        or state.scope != scope_hash
        or method is not None
        and state.method is not None
        and method != state.method
    ):
        state = Episode(
            scope=scope_hash,
            method=method,
            baseline=(
                previous_valid is True
                or state is not None
                and state.baseline
                or state is None
                and row is not None
            ),
        )
    elif method is not None:
        state.method = method
    before = row.payload if row is not None else None

    def event(current: bool) -> EdgeV2Transition:
        result = EdgeV2Transition(
            site_id=observation.site_id,
            edge_id=observation.edge_id,
            occurred_at=observation.generated_at,
            kind="MEASUREMENT_VALIDITY_CHANGED",
            payload={
                "previousValid": previous_valid,
                "currentValid": current,
                "pitId": pit_id,
                "simulated": observation.simulated,
                "assessmentId": observation.assessment_id
                if observation.source == "EDGE_STATE"
                else None,
                "observationId": observation.observation_id,
                "inputSource": observation.source,
                "messageId": observation.message_id,
                "reasonCodes": observation.reason_codes,
                "measurementEpisodeEvent": True,
            },
        )
        db.add(result)
        return result

    if not valid:
        # An invalid observation cancels confirmation even when no boolean changed.
        state.observed_since = state.received_since = None
        state.last_observed = state.last_generated = state.last_received = None
        causes = {digest(code) for code in observation.reason_codes if code not in PROVENANCE} or {
            digest("INPUT_UNAVAILABLE")
        }
        known = set(state.causes)
        if len(known | causes) > MAX_CAUSES:
            # Insufficient bounded evidence must never justify hiding a new error.
            known = set()
        if state.baseline and not causes <= known:
            public = transition if transition is not None else event(False)
            public.payload = {**public.payload, "measurementEpisodeEvent": True}
            # Incoming reason codes are schema-bounded; keep a bounded suppression set.
            state.causes = sorted(known | causes)[:MAX_CAUSES]
        elif transition is not None:
            transition.payload = {
                **transition.payload,
                "diagnosticOnly": True,
                "measurementEpisodeContinuation": True,
            }
    elif not state.causes:
        # Startup without a valid baseline does not invent an error/recovery pair.
        state.baseline = True
        if transition is not None:
            transition.payload = {
                **transition.payload,
                "diagnosticOnly": True,
                "measurementRecoveryPending": True,
            }
    else:
        if transition is not None:
            transition.payload = {
                **transition.payload,
                "diagnosticOnly": True,
                "measurementRecoveryPending": True,
            }
        observed, generated, received = (
            observation.observed_to,
            observation.generated_at,
            observation.received_at,
        )
        trusted = observed is not None and observed <= generated <= received
        continuous = (
            trusted
            and state.last_observed is not None
            and state.last_generated is not None
            and state.last_received is not None
            and observed is not None
            and timedelta(0) < observed - state.last_observed <= MAX_GAP
            and generated > state.last_generated
            and timedelta(0) < received - state.last_received <= MAX_GAP
        )
        if not continuous:
            state.observed_since = observed if trusted else None
            state.received_since = received if trusted else None
        state.last_observed = observed if trusted else None
        state.last_generated = generated if trusted else None
        state.last_received = received if trusted else None
        if (
            observed is not None
            and state.observed_since is not None
            and state.received_since is not None
            and observed - state.observed_since >= timedelta(seconds=RECOVERY_SECONDS)
            and received - state.received_since >= timedelta(seconds=RECOVERY_SECONDS)
        ):
            recovery = event(True)
            recovery.payload = {
                **recovery.payload,
                "recoveryConfirmSeconds": RECOVERY_SECONDS,
                "recoveryObservedFrom": state.observed_since.isoformat(),
                "recoveryReceivedFrom": state.received_since.isoformat(),
            }
            state = Episode(scope=scope_hash, method=state.method, baseline=True)

    payload = state.model_dump(mode="json")
    # Normal periodic input incurs no additional UPDATE after its baseline exists.
    if payload == before:
        return
    if row is None:
        db.add(
            EdgeV2Latest(
                site_id=observation.site_id,
                edge_id=observation.edge_id,
                kind=key[2],
                message_id=observation.message_id,
                generated_at=observation.generated_at,
                payload=payload,
            )
        )
    else:
        row.payload = payload
        row.message_id = observation.message_id
        row.generated_at = observation.generated_at
