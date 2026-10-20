"""Cumulative height-drop detection, driven only by new valid worker observations."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.collection_detection import CollectionTracker
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.edge_v2_service import digest, utc
from src.app.services.height_fill_service import height_method
from src.app.services.height_reference import height_reference, reference_depth
from src.app.services.monitoring_v2_service import build_snapshot

TRACKER_KIND = "collection_tracker"
MAX_GAP_SECONDS = 3


def collection_scope(source: MonitoringSource, pit: PitConfig | None) -> str | None:
    settings = get_settings()
    method = height_method(source, pit.total_depth_cm if pit else None)
    if method is None or settings.EDGE_V2_COLLECTION_DROP_METERS is None:
        return None
    return digest(
        [
            source.model_dump(),
            method,
            settings.EDGE_V2_COLLECTION_DROP_METERS,
            settings.EDGE_V2_COLLECTION_STABLE_BAND_METERS,
            settings.EDGE_V2_COLLECTION_CONFIRM_SECONDS,
            settings.EDGE_V2_COLLECTION_BASELINE_SECONDS,
            settings.EDGE_V2_COLLECTION_SETTLE_SECONDS,
        ]
    )


def invalidate(state: CollectionTracker) -> None:
    """An outage is never elapsed stable time and never confirms a fresh drop."""
    state.input_valid = False
    state.stable_since = state.stable_min_cm = state.stable_max_cm = None
    state.candidate_since = None
    if state.phase != "COLLECTING":
        state.phase = "BASELINING"
        state.baseline_cm = None


def advance(
    state: CollectionTracker, height_cm: float, observed: datetime
) -> list[tuple[str, dict[str, object]]]:
    settings = get_settings()
    assert settings.EDGE_V2_COLLECTION_DROP_METERS is not None
    drop = settings.EDGE_V2_COLLECTION_DROP_METERS * 100.0
    band = settings.EDGE_V2_COLLECTION_STABLE_BAND_METERS * 100.0
    events: list[tuple[str, dict[str, object]]] = []
    low = min(state.stable_min_cm, height_cm) if state.stable_min_cm is not None else height_cm
    high = max(state.stable_max_cm, height_cm) if state.stable_max_cm is not None else height_cm
    if state.stable_since is None or high - low > band:
        state.stable_since, low, high = observed, height_cm, height_cm
    state.stable_min_cm, state.stable_max_cm = low, high
    stable_seconds = (observed - state.stable_since).total_seconds()
    stable_height = (low + high) / 2
    state.current_cm, state.observed_at, state.input_valid = height_cm, observed, True
    if state.phase == "BASELINING":
        if stable_seconds >= settings.EDGE_V2_COLLECTION_BASELINE_SECONDS:
            state.baseline_cm, state.phase = stable_height, "IDLE"
        return events
    assert state.baseline_cm is not None
    if state.phase == "COLLECTING":
        if stable_seconds >= settings.EDGE_V2_COLLECTION_SETTLE_SECONDS:
            events.append(
                (
                    "COLLECTION_SETTLED",
                    {
                        "detectionId": state.episode_id,
                        "heightCm": height_cm,
                        "confirmedCompletion": False,
                    },
                )
            )
            state.baseline_cm, state.phase = stable_height, "IDLE"
            state.episode_id = None
            state.candidate_since = None
        return events
    if (
        height_cm > state.baseline_cm
        and stable_seconds >= settings.EDGE_V2_COLLECTION_BASELINE_SECONDS
    ):
        state.baseline_cm = max(state.baseline_cm, stable_height)
    if state.baseline_cm - height_cm < drop:
        state.candidate_since, state.phase = None, "IDLE"
        return events
    if state.candidate_since is None:
        state.candidate_since, state.phase = observed, "CANDIDATE"
    if observed - state.candidate_since >= timedelta(
        seconds=settings.EDGE_V2_COLLECTION_CONFIRM_SECONDS
    ):
        state.episode_id = "detected-" + digest([state.scope_token, observed.isoformat()])
        state.phase = "COLLECTING"
        # Stable time for ending the episode starts after detection, never before it.
        state.stable_since, state.stable_min_cm, state.stable_max_cm = (
            observed,
            height_cm,
            height_cm,
        )
        events.append(
            (
                "COLLECTION_DETECTED",
                {
                    "detectionId": state.episode_id,
                    "baselineHeightCm": state.baseline_cm,
                    "heightCm": height_cm,
                    "dropCm": state.baseline_cm - height_cm,
                    "confirmedCompletion": False,
                },
            )
        )
    return events


async def process_collection_source(
    db: AsyncSession, source: MonitoringSource, now: datetime | None = None
) -> None:
    settings = get_settings()
    if not settings.EDGE_V2_COLLECTION_DETECTION_ENABLED:
        return
    now = now or datetime.now(UTC)
    row = await db.scalar(
        select(EdgeV2Latest)
        .where(
            EdgeV2Latest.site_id == source.site_id,
            EdgeV2Latest.edge_id == source.edge_id,
            EdgeV2Latest.kind == TRACKER_KIND,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    snapshot = await build_snapshot(db, source, now)
    pit = await db.get(PitConfig, source.pit_id)
    scope = collection_scope(source, pit)
    depth = reference_depth(height_reference(source.pit_id), pit.total_depth_cm if pit else None)
    observation = snapshot.fillObservation
    valid = (
        scope is not None
        and depth is not None
        and snapshot.dataState == "FRESH"
        and snapshot.summary.filterState == "READY"
        and snapshot.summary.loadPercent is not None
        and observation is not None
        and observation.observed_to is not None
        and observation.source == "HEIGHT_SECTIONS"
    )
    state = CollectionTracker.model_validate(row.payload) if row else None
    if state and (state.pit_id != source.pit_id or state.scope_token != scope):
        state = None
    if not valid:
        if state and state.input_valid:
            invalidate(state)
            assert row is not None
            row.payload = state.model_dump(mode="json")
            await db.commit()
        return
    assert observation is not None and observation.observed_to is not None
    assert depth is not None and scope is not None and snapshot.summary.loadPercent is not None
    observed = observation.observed_to
    if (
        row
        and state
        and (
            observed <= utc(state.observed_at) or observation.generated_at <= utc(row.generated_at)
        )
    ):
        return
    if state is None:
        state = CollectionTracker(
            pit_id=source.pit_id,
            scope_token=scope,
            observed_at=observed,
            simulated=observation.simulated,
        )
    elif observed - utc(state.observed_at) > timedelta(seconds=MAX_GAP_SECONDS):
        invalidate(state)
    events = advance(state, snapshot.summary.loadPercent * depth / 100.0, observed)
    for kind, detail in events:
        db.add(
            EdgeV2Transition(
                site_id=source.site_id,
                edge_id=source.edge_id,
                occurred_at=observed,
                kind=kind,
                payload={
                    **detail,
                    "pitId": source.pit_id,
                    "scopeToken": scope,
                    "simulated": state.simulated,
                    "methodVersion": observation.method_version,
                    "observationId": observation.observation_id,
                    "timeBasis": observation.time_basis,
                },
            )
        )
    if row is None:
        row = EdgeV2Latest(
            site_id=source.site_id,
            edge_id=source.edge_id,
            kind=TRACKER_KIND,
            message_id=observation.message_id,
            generated_at=observation.generated_at,
            payload=state.model_dump(mode="json"),
        )
        db.add(row)
    else:
        row.message_id, row.generated_at = observation.message_id, observation.generated_at
        row.payload = state.model_dump(mode="json")
    await db.commit()
