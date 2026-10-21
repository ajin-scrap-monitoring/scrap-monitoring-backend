"""Cumulative height-drop detection, driven only by new valid worker observations."""

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.collection_detection import CollectionTracker
from src.app.schemas.fill_observation import FillObservation
from src.app.schemas.lidar_height import SimulationHeightReference
from src.app.schemas.monitoring_v2 import MonitoringSnapshotV2, MonitoringSource
from src.app.services.edge_v2_service import digest, utc
from src.app.services.height_fill_service import height_method
from src.app.services.height_median_service import HeightWindow, window_scope
from src.app.services.height_reference import height_reference, reference_depth, reference_sensors
from src.app.services.monitoring_v2_service import build_snapshot

TRACKER_KIND = "collection_tracker"
MAX_GAP_SECONDS = 3
DETECTION_VERSION = "UPPER_RISING_BASELINE_V3"


def collection_profile(source: MonitoringSource) -> Literal["FIELD", "SIMULATION"]:
    """Only explicit private simulation geometry selects short timing, never drop speed."""
    return (
        "SIMULATION"
        if isinstance(height_reference(source.pit_id), SimulationHeightReference)
        else "FIELD"
    )


def collection_settle_seconds(source: MonitoringSource) -> int:
    settings = get_settings()
    return (
        settings.EDGE_V2_COLLECTION_SIMULATION_SETTLE_SECONDS
        if collection_profile(source) == "SIMULATION"
        else settings.EDGE_V2_COLLECTION_SETTLE_SECONDS
    )


async def collection_height(
    db: AsyncSession,
    source: MonitoringSource,
    observation: FillObservation,
    representative_cm: float,
    now: datetime,
) -> float | None:
    """Median of per-scan maxima, then maximum across the configured sensors (cm).

    This is a stable observed upper height, not an unseen whole-pit maximum. A remaining
    high scrap point or sustained arm occlusion can still delay or bias detection.
    """
    if get_settings().EDGE_V2_COLLECTION_HEIGHT_BASIS == "REPRESENTATIVE":
        return representative_cm
    reference = height_reference(source.pit_id)
    row = await db.get(EdgeV2Latest, (source.site_id, source.edge_id, "height_window"))
    if reference is None or row is None or row.message_id != observation.message_id:
        return None
    window = HeightWindow.model_validate(row.payload)
    if window.scope != await window_scope(db, source, now):
        return None
    size = get_settings().EDGE_V2_MEDIAN_WINDOW_SECONDS
    values: list[float] = []
    latest_times: list[datetime] = []
    for sensor in reference_sensors(reference):
        history = window.sensors.get(sensor)
        if history is None or len(history.frames) != size:
            return None
        if any(frame.maximum_cm is None for frame in history.frames):
            return None
        values.append(
            float(
                median(frame.maximum_cm for frame in history.frames if frame.maximum_cm is not None)
            )
        )
        latest_times.append(history.frames[-1].at)
    if not values or max(latest_times) != observation.observed_to:
        return None
    return max(values)


def collection_scope(source: MonitoringSource, pit: PitConfig | None) -> str | None:
    settings = get_settings()
    method = height_method(source, pit.total_depth_cm if pit else None)
    if method is None or settings.EDGE_V2_COLLECTION_DROP_METERS is None:
        return None
    return digest(
        [
            DETECTION_VERSION,
            source.model_dump(),
            method,
            settings.EDGE_V2_COLLECTION_HEIGHT_BASIS,
            settings.EDGE_V2_COLLECTION_DROP_METERS,
            settings.EDGE_V2_COLLECTION_STABLE_BAND_METERS,
            settings.EDGE_V2_COLLECTION_CONFIRM_SECONDS,
            settings.EDGE_V2_COLLECTION_BASELINE_SECONDS,
            collection_settle_seconds(source),
        ]
    )


def invalidate(state: CollectionTracker) -> None:
    """An outage is never elapsed stable time and never confirms a fresh drop."""
    state.input_valid = False
    state.stable_since = state.stable_min_cm = state.stable_max_cm = None
    state.candidate_since = None
    state.quiet_since = state.progress_anchor_cm = state.progress_candidate_since = None
    state.reference_candidate_cm = state.reference_candidate_since = None
    state.reference_peak_cm = None
    if state.phase != "COLLECTING":
        state.phase = "BASELINING"
        state.baseline_cm = None


def sustained_reference(
    state: CollectionTracker, height_cm: float, observed: datetime, band: float
) -> float | None:
    """Confirm a level from below while loading continues, using bounded scalars.

    A rising surface need not fit in a flat stability band. The candidate level must
    remain supported for the full baseline interval; a drop beyond the same noise
    band restarts preparation. A brief upper spike therefore cannot raise the reference.
    """
    if (
        state.reference_candidate_cm is None
        or state.reference_candidate_since is None
        or state.reference_peak_cm is None
        or height_cm < state.reference_peak_cm - band
    ):
        state.reference_candidate_cm, state.reference_candidate_since = height_cm, observed
        state.reference_peak_cm = height_cm
    state.reference_peak_cm = max(state.reference_peak_cm, height_cm)
    if observed - state.reference_candidate_since < timedelta(
        seconds=get_settings().EDGE_V2_COLLECTION_BASELINE_SECONDS
    ):
        return None
    accepted = state.reference_candidate_cm
    state.reference_candidate_cm, state.reference_candidate_since = height_cm, observed
    state.reference_peak_cm = height_cm
    return accepted


def advance(
    state: CollectionTracker,
    height_cm: float,
    observed: datetime,
    settle_seconds: int | None = None,
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
    supported_height = (
        sustained_reference(state, height_cm, observed, band)
        if state.phase != "COLLECTING"
        else None
    )
    if state.phase == "BASELINING":
        if stable_seconds >= settings.EDGE_V2_COLLECTION_BASELINE_SECONDS:
            state.baseline_cm, state.phase = stable_height, "IDLE"
        elif supported_height is not None:
            state.baseline_cm, state.phase = supported_height, "IDLE"
        return events
    assert state.baseline_cm is not None
    if state.phase == "COLLECTING":
        # Loading may resume while work pauses. Rising height does not require a flat
        # plateau; only a sustained further drop restarts the quiet interval.
        if state.progress_anchor_cm is None or state.quiet_since is None:
            state.progress_anchor_cm, state.quiet_since = height_cm, observed
        if (
            height_cm > state.progress_anchor_cm
            and stable_seconds >= settings.EDGE_V2_COLLECTION_BASELINE_SECONDS
        ):
            state.progress_anchor_cm = max(state.progress_anchor_cm, stable_height)
        if state.progress_anchor_cm - height_cm >= band:
            state.progress_candidate_since = state.progress_candidate_since or observed
            if observed - state.progress_candidate_since >= timedelta(
                seconds=settings.EDGE_V2_COLLECTION_CONFIRM_SECONDS
            ):
                state.progress_anchor_cm, state.quiet_since = height_cm, observed
                state.progress_candidate_since = None
        else:
            state.progress_candidate_since = None
        wait = (
            settle_seconds
            if settle_seconds is not None
            else settings.EDGE_V2_COLLECTION_SETTLE_SECONDS
        )
        if state.progress_candidate_since is None and observed - state.quiet_since >= timedelta(
            seconds=wait
        ):
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
            state.quiet_since = state.progress_anchor_cm = state.progress_candidate_since = None
            state.reference_candidate_cm = state.reference_candidate_since = None
            state.reference_peak_cm = None
        return events
    if supported_height is not None:
        state.baseline_cm = max(state.baseline_cm, supported_height)
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
        # Quiet time for ending the episode starts after detection, never before it.
        state.quiet_since, state.progress_anchor_cm = observed, height_cm
        state.progress_candidate_since = None
        state.reference_candidate_cm = state.reference_candidate_since = None
        state.reference_peak_cm = None
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


async def read_collection_input(
    db: AsyncSession, source: MonitoringSource, now: datetime
) -> tuple[MonitoringSnapshotV2, str | None, float | None, float | None]:
    """Read all height/context components in the caller's database snapshot."""
    snapshot = await build_snapshot(db, source, now)
    pit = await db.get(PitConfig, source.pit_id)
    scope = collection_scope(source, pit)
    depth = reference_depth(height_reference(source.pit_id), pit.total_depth_cm if pit else None)
    observation = snapshot.fillObservation
    height_cm = (
        await collection_height(
            db, source, observation, snapshot.summary.loadPercent * depth / 100.0, now
        )
        if observation is not None
        and depth is not None
        and snapshot.summary.loadPercent is not None
        else None
    )
    return snapshot, scope, depth, height_cm


async def collection_input(
    db: AsyncSession, source: MonitoringSource, now: datetime
) -> tuple[MonitoringSnapshotV2, str | None, float | None, float | None]:
    """Keep concurrent receiver commits from mixing two observation generations.

    PostgreSQL's default READ COMMITTED can see a newer height window after reading
    the observation/filter. A short, independent read-only snapshot avoids false
    invalidation without locking receiver rows or extending any freshness allowance.
    The caller still owns the tracker lock and atomic state/event write transaction.
    """
    if db.get_bind().dialect.name != "postgresql":
        return await read_collection_input(db, source, now)
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as reading:
        await reading.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        await reading.execute(text("SET TRANSACTION READ ONLY"))
        return await read_collection_input(reading, source, now)


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
    snapshot, scope, depth, height_cm = await collection_input(db, source, now)
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
        and height_cm is not None
        and observation.simulated == (collection_profile(source) == "SIMULATION")
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
    assert height_cm is not None
    events = advance(state, height_cm, observed, collection_settle_seconds(source))
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
                    "detectionVersion": DETECTION_VERSION,
                    "heightBasis": settings.EDGE_V2_COLLECTION_HEIGHT_BASIS,
                    "detectionProfile": collection_profile(source),
                    "settleSeconds": collection_settle_seconds(source),
                    "endPolicy": "NO_FURTHER_DROP",
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
