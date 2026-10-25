"""Bounded current episode pointer and small durable alert/recovery evidence."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.schemas.alert_lifecycle import AlertLifecycle
from src.app.schemas.fill_observation import FillObservation
from src.app.schemas.monitoring_v2 import MonitoringSnapshotV2, MonitoringSource
from src.app.services.edge_v2_service import digest, utc

TRACKER_KIND = "alert_lifecycle"
CREATED = "ALERT_CREATED"
RESOLVED = "ALERT_RESOLVED"


def stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


def lifecycle_scope(snapshot: MonitoringSnapshotV2) -> str:
    observation, edge = snapshot.fillObservation, snapshot.assessment
    status = snapshot.deviceStatus.snapshot
    return digest(
        [
            snapshot.source.model_dump(),
            observation.config_revision if observation else edge.config_revision if edge else None,
            observation.rule_version if observation else edge.rule_version if edge else None,
            observation.method_version
            if observation
            else edge.fill_estimate.method_version
            if edge and edge.fill_estimate
            else None,
            observation.scope_token if observation else None,
            snapshot.summary.inputSource,
            snapshot.summary.simulated,
            snapshot.summary.warningThresholdPercent,
            snapshot.summary.collectionThresholdPercent,
            snapshot.summary.recoveryThresholdPercent,
            status.orchestrator_instance_id if status else None,
        ]
    )


async def tracker(db: AsyncSession, source: MonitoringSource) -> EdgeV2Latest | None:
    return await db.get(
        EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND), populate_existing=True
    )


async def invalidate_episode(db: AsyncSession, source: MonitoringSource) -> bool:
    row = await tracker(db, source)
    if row is None or row.payload.get("pitId") != source.pit_id:
        return False
    if row.payload.get("episodeId") is None and row.payload.get("inputValid") is False:
        return False
    # A data gap is not a recovery. Unclosed old episodes remain unknown.
    row.payload = {**row.payload, "episodeId": None, "inputValid": False}
    return True


@dataclass(frozen=True)
class Episode:
    identifier: str
    scope: str
    row: EdgeV2Latest


async def advance_episode(
    db: AsyncSession,
    source: MonitoringSource,
    snapshot: MonitoringSnapshotV2,
    observation: FillObservation,
    previous: str,
    current: str,
    now: datetime,
) -> Episode | None:
    observed = observation.observed_to
    assert observed is not None
    row = await tracker(db, source)
    old = row.payload if row else {}
    scope = lifecycle_scope(snapshot)
    before = stamp(old.get("observedAt"))
    compatible = (
        old.get("pitId") == source.pit_id
        and old.get("scopeToken") == scope
        and old.get("inputValid") is True
        and before is not None
        and timedelta(0)
        < observed - before
        < timedelta(seconds=get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS)
    )
    identifier = old.get("episodeId") if compatible else None
    identifier = identifier if isinstance(identifier, str) else None
    has_alert = old.get("hasAlert") is True if identifier else False
    if current == "CRITICAL":
        identifier = identifier or uuid4().hex
    else:
        if identifier and has_alert and previous == "CRITICAL":
            db.add(
                EdgeV2Transition(
                    site_id=source.site_id,
                    edge_id=source.edge_id,
                    occurred_at=now,
                    kind=RESOLVED,
                    payload={
                        "pitId": source.pit_id,
                        "episodeId": identifier,
                        "scopeToken": scope,
                        "resolutionObservedAt": observed.isoformat(),
                    },
                )
            )
        identifier, has_alert = None, False
    if row is None:
        if identifier is None:
            return None
        row = EdgeV2Latest(site_id=source.site_id, edge_id=source.edge_id, kind=TRACKER_KIND)
        db.add(row)
    row.message_id, row.generated_at = observation.message_id, observation.generated_at
    row.payload = {
        "pitId": source.pit_id,
        "scopeToken": scope,
        "episodeId": identifier,
        "observedAt": observed.isoformat(),
        "inputValid": True,
        "hasAlert": has_alert,
    }
    return Episode(identifier, scope, row) if identifier else None


def record_alert(
    db: AsyncSession,
    source: MonitoringSource,
    episode: Episode,
    alert_id: int,
    observation: FillObservation,
    now: datetime,
) -> None:
    assert observation.observed_to is not None
    episode.row.payload = {**episode.row.payload, "hasAlert": True}
    db.add(
        EdgeV2Transition(
            site_id=source.site_id,
            edge_id=source.edge_id,
            occurred_at=now,
            kind=CREATED,
            payload={
                "pitId": source.pit_id,
                "alertId": alert_id,
                "episodeId": episode.identifier,
                "scopeToken": episode.scope,
                "observedAt": observation.observed_to.isoformat(),
                "simulated": observation.simulated,
            },
        )
    )


async def current_episode(
    db: AsyncSession, pit_id: str, now: datetime
) -> tuple[str, str, str, str] | None:
    from src.app.services.monitoring_v2_service import build_snapshot

    settings = get_settings()
    source = next((s for s in settings.EDGE_V2_MONITORING_SOURCES if s.pit_id == pit_id), None)
    if not settings.EDGE_V2_OPERATIONS_ENABLED or source is None:
        return None
    snapshot = await build_snapshot(db, source, now)
    if (
        snapshot.dataState != "FRESH"
        or not snapshot.operations
        or snapshot.operations.effectiveState != "CRITICAL"
    ):
        return None
    row = await tracker(db, source)
    payload = row.payload if row else {}
    observed, episode, scope = (
        stamp(payload.get("observedAt")),
        payload.get("episodeId"),
        payload.get("scopeToken"),
    )
    if (
        payload.get("pitId") == pit_id
        and payload.get("inputValid") is True
        and isinstance(episode, str)
        and isinstance(scope, str)
        and observed is not None
        and timedelta(0)
        <= now - observed
        < timedelta(seconds=settings.EDGE_V2_ASSESSMENT_TTL_SECONDS)
        and scope == lifecycle_scope(snapshot)
    ):
        return source.site_id, source.edge_id, episode, scope
    return None


async def alert_lifecycles(
    db: AsyncSession,
    pit_id: str,
    alert_ids: list[int],
    now: datetime | None = None,
) -> dict[int, AlertLifecycle]:
    """Resolve only explicit evidence; old alerts and interrupted episodes stay unknown."""
    now = now or datetime.now(UTC)
    result = {identifier: AlertLifecycle() for identifier in alert_ids}
    if not alert_ids:
        return result
    event, link = EdgeV2Transition, EdgeV2AlertLink
    # Text comparison cannot raise a PostgreSQL numeric-cast error for malformed old JSON.
    rows = (
        await db.execute(
            select(link.alert_id, event)
            .join(
                event,
                and_(
                    cast(event.payload["alertId"].as_string(), String)
                    == cast(link.alert_id, String),
                    event.site_id == link.site_id,
                    event.edge_id == link.edge_id,
                ),
            )
            .where(
                link.pit_id == pit_id,
                link.alert_id.in_(alert_ids),
                event.kind == CREATED,
                event.payload["pitId"].as_string() == pit_id,
                event.occurred_at <= now,
            )
            .order_by(event.occurred_at, event.id)
        )
    ).all()
    created: dict[int, EdgeV2Transition] = {}
    for identifier, row in rows:
        if isinstance(row.payload.get("episodeId"), str) and isinstance(
            row.payload.get("scopeToken"), str
        ):
            created.setdefault(identifier, row)
    if not created:
        return result
    sources = {(row.site_id, row.edge_id) for row in created.values()}
    resolutions = await db.scalars(
        select(event)
        .where(
            event.kind == RESOLVED,
            event.payload["pitId"].as_string() == pit_id,
            event.payload["episodeId"]
            .as_string()
            .in_([r.payload["episodeId"] for r in created.values()]),
            event.occurred_at <= now,
            or_(*(and_(event.site_id == site, event.edge_id == edge) for site, edge in sources)),
        )
        .order_by(event.occurred_at, event.id)
    )
    resolved: dict[tuple[str, str, str, str], EdgeV2Transition] = {}
    for row in resolutions:
        episode, scope = row.payload.get("episodeId"), row.payload.get("scopeToken")
        if isinstance(episode, str) and isinstance(scope, str):
            resolved.setdefault((row.site_id, row.edge_id, episode, scope), row)
    active = await current_episode(db, pit_id, now)
    for identifier, row in created.items():
        key = row.site_id, row.edge_id, row.payload["episodeId"], row.payload["scopeToken"]
        end = resolved.get(key)
        if end is not None:
            observed = stamp(end.payload.get("resolutionObservedAt"))
            began = stamp(row.payload.get("observedAt"))
            at = utc(end.occurred_at)
            if (
                observed is not None
                and began is not None
                and began < observed <= at
                and utc(row.occurred_at) <= at
            ):
                result[identifier] = AlertLifecycle(
                    state="RESOLVED", resolvedAt=at, resolutionObservedAt=observed
                )
        elif key == active:
            result[identifier] = AlertLifecycle(state="ACTIVE")
    return result
