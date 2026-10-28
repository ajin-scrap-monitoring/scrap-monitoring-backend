"""Bounded event metadata queries for shared daily recording playback."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    DateTime,
    String,
    and_,
    cast,
    func,
    literal,
    or_,
    outerjoin,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.models.log import AlertLog, CollectionLog
from src.app.models.metric import ScrapMetric
from src.app.schemas.collection_detection import CollectionTracker
from src.app.schemas.event_recording import EventRecording, ReferenceCategory
from src.app.schemas.monitoring_v2 import MonitoringSource
from src.app.services.collection_detection_service import (
    MAX_GAP_SECONDS,
    TRACKER_KIND,
    collection_scope,
)
from src.app.services.edge_v2_service import utc
from src.app.services.error_history_service import error_transition_event
from src.app.services.media_client import MediaServiceError


def playback_scope(pit_id: str | None) -> tuple[str, MonitoringSource | None]:
    settings = get_settings()
    if not settings.MEDIA_DAILY_SERVICE_URL and not settings.MEDIA_SERVICE_URL:
        raise MediaServiceError(503, "MEDIA_DAILY_NOT_CONFIGURED")
    pit = pit_id or settings.MEDIA_PIT_ID
    if pit != settings.MEDIA_PIT_ID:
        raise HTTPException(404, "CAMERA_NOT_CONFIGURED")
    return pit, next((s for s in settings.EDGE_V2_MONITORING_SOURCES if s.pit_id == pit), None)


def shifted(value: datetime, seconds: int) -> datetime:
    try:
        return value + timedelta(seconds=seconds)
    except OverflowError:
        return (
            datetime.min.replace(tzinfo=value.tzinfo)
            if seconds < 0
            else datetime.max.replace(tzinfo=value.tzinfo)
        )


async def active_collection(
    db: AsyncSession, source: MonitoringSource | None, now: datetime
) -> tuple[str, str] | None:
    if source is None or not get_settings().EDGE_V2_COLLECTION_DETECTION_ENABLED:
        return None
    row = await db.get(
        EdgeV2Latest, (source.site_id, source.edge_id, TRACKER_KIND), populate_existing=True
    )
    if row is None:
        return None
    try:
        state = CollectionTracker.model_validate(row.payload)
    except ValidationError:
        return None
    pit = await db.get(PitConfig, source.pit_id)
    if (
        state.pit_id == source.pit_id
        and state.phase == "COLLECTING"
        and state.input_valid
        and state.episode_id
        and state.scope_token == collection_scope(source, pit)
        and timedelta(0) <= now - utc(state.observed_at) <= timedelta(seconds=MAX_GAP_SECONDS)
    ):
        return state.episode_id, state.scope_token
    return None


def _json_boolean(db: AsyncSession, payload: Any, key: str, value: bool) -> Any:
    if db.get_bind().dialect.name == "postgresql":
        return and_(
            func.json_typeof(cast(payload[key], JSON)) == "boolean",
            payload[key].as_string() == ("true" if value else "false"),
        )
    return func.json_type(payload, "$." + key) == ("true" if value else "false")


def event_table(
    db: AsyncSession,
    pit: str,
    source: MonitoringSource | None,
    category: str,
    lower: datetime | None,
    upper: datetime | None,
    now: datetime,
    before: int,
    after: int,
    active: tuple[str, str] | None,
    only_id: str | None = None,
    only_ids: set[str] | None = None,
) -> Subquery:
    prefix, number = "", 0
    if only_id is not None:
        prefix, _, raw = only_id.rpartition("-")
        if not raw.isascii() or not raw.isdecimal() or raw.startswith("0"):
            raise HTTPException(404, "EVENT_NOT_FOUND")
        number = int(raw)
        if number > 9223372036854775807:
            raise HTTPException(404, "EVENT_NOT_FOUND")
    parts = []

    def add(
        model: Any,
        id_prefix: str,
        kind: str,
        occurred: Any,
        filters: list[Any],
        payload: Any = None,
        ended: Any = None,
        cycle: bool = False,
        from_clause: Any = None,
    ) -> None:
        if only_id is not None and prefix != id_prefix:
            return
        if only_id is not None:
            filters = [*filters, model.id == number]
        if only_ids is not None:
            # History enrichment checks only the requested page, using primary keys.
            numbers = [
                int(raw)
                for identifier in only_ids
                for candidate, _, raw in [identifier.rpartition("-")]
                if candidate == id_prefix
                and raw.isascii()
                and raw.isdecimal()
                and not raw.startswith("0")
                and len(raw) <= 19
                and int(raw) <= 9223372036854775807
            ]
            if not numbers:
                return
            filters = [*filters, model.id.in_(numbers)]
        if lower is not None:
            if cycle:
                ongoing = and_(ended.is_(None), occurred >= lower)
                if active is not None and lower < now:
                    ongoing = or_(
                        ongoing,
                        and_(
                            ended.is_(None),
                            model.payload["detectionId"].as_string() == active[0],
                            model.payload["scopeToken"].as_string() == active[1],
                        ),
                    )
                filters = [*filters, or_(ended > lower, ongoing)]
            else:
                filters = [*filters, occurred > shifted(lower, -after)]
        if upper is not None:
            filters = [*filters, occurred < (upper if cycle else shifted(upper, before))]
        statement = select(
            (literal(id_prefix + "-") + cast(model.id, String)).label("event_id"),
            model.id.label("number"),
            literal(kind).label("kind"),
            occurred.label("at"),
            (ended if ended is not None else cast(literal(None), DateTime(timezone=True))).label(
                "ended"
            ),
            (payload if payload is not None else cast(literal(None), JSON)).label("payload"),
        ).where(*filters, occurred <= now)
        if from_clause is not None:
            statement = statement.select_from(from_clause)
        parts.append(statement)

    if category in ("all", "collection"):
        if source is not None:
            start, end = aliased(EdgeV2Transition), aliased(EdgeV2Transition)
            detection_key = end.payload["detectionId"].as_string()
            scope_key = end.payload["scopeToken"].as_string()
            # Group once instead of scanning every later end for each start row.
            ends = (
                select(
                    detection_key.label("detection"),
                    scope_key.label("scope"),
                    func.min(end.occurred_at).label("ended"),
                )
                .where(
                    end.site_id == source.site_id,
                    end.edge_id == source.edge_id,
                    end.kind == "COLLECTION_SETTLED",
                    end.payload["pitId"].as_string() == pit,
                    end.occurred_at <= now,
                )
                .group_by(detection_key, scope_key)
                .subquery()
            )
            ended = ends.c.ended
            joined = outerjoin(
                start,
                ends,
                and_(
                    ends.c.detection == start.payload["detectionId"].as_string(),
                    ends.c.scope == start.payload["scopeToken"].as_string(),
                    ends.c.ended >= start.occurred_at,
                ),
            )
            add(
                start,
                "evt-auto-col-start",
                "COLLECTION",
                start.occurred_at,
                [
                    start.site_id == source.site_id,
                    start.edge_id == source.edge_id,
                    start.kind == "COLLECTION_DETECTED",
                    start.payload["pitId"].as_string() == pit,
                ],
                start.payload,
                ended,
                True,
                joined,
            )
        add(
            CollectionLog,
            "evt-col",
            "MANUAL_COLLECTION",
            CollectionLog.completed_at,
            [CollectionLog.pit_id == pit],
        )
    if category in ("all", "alert"):
        created = aliased(EdgeV2Transition)
        payload = None
        if source is not None:
            payload = (
                select(created.payload)
                .join(
                    EdgeV2AlertLink,
                    and_(
                        created.site_id == EdgeV2AlertLink.site_id,
                        created.edge_id == EdgeV2AlertLink.edge_id,
                        cast(created.payload["alertId"].as_string(), String)
                        == cast(EdgeV2AlertLink.alert_id, String),
                    ),
                )
                .where(
                    EdgeV2AlertLink.alert_id == AlertLog.id,
                    EdgeV2AlertLink.pit_id == pit,
                    EdgeV2AlertLink.site_id == source.site_id,
                    EdgeV2AlertLink.edge_id == source.edge_id,
                    created.kind == "ALERT_CREATED",
                    created.payload["pitId"].as_string() == pit,
                )
                .correlate(AlertLog)
                .order_by(created.id)
                .limit(1)
                .scalar_subquery()
            )
        add(AlertLog, "evt-alt", "ALERT", AlertLog.triggered_at, [AlertLog.pit_id == pit], payload)
    if category in ("all", "error"):
        add(
            ScrapMetric,
            "evt-err",
            "LEGACY_ERROR",
            ScrapMetric.measured_at,
            [ScrapMetric.pit_id == pit, ScrapMetric.is_valid.is_(False)],
        )
        if source is not None:
            event = EdgeV2Transition
            previous, current = (
                event.payload["previous"].as_string(),
                event.payload["current"].as_string(),
            )
            device = and_(
                event.kind == "DEVICE_STATE_CHANGED",
                current.in_(
                    ["MEASUREMENT_UNAVAILABLE", "CONFIG_ERROR", "HEALTHY", "DEGRADED", "UNKNOWN"]
                ),
                or_(previous.is_(None), previous != current),
            )
            changed = or_(
                and_(
                    _json_boolean(db, event.payload, "previousValid", True),
                    _json_boolean(db, event.payload, "currentValid", False),
                ),
                and_(
                    _json_boolean(db, event.payload, "previousValid", False),
                    _json_boolean(db, event.payload, "currentValid", True),
                ),
            )
            filters = [
                event.site_id == source.site_id,
                event.edge_id == source.edge_id,
                or_(
                    event.payload["pitId"].as_string() == pit,
                    event.payload["pitId"].as_string().is_(None),
                ),
            ]
            # Separate branches preserve each recorded transition kind for presentation.
            for kind, condition in [
                ("DEVICE_STATE_CHANGED", device),
                (
                    "MEASUREMENT_VALIDITY_CHANGED",
                    and_(event.kind == "MEASUREMENT_VALIDITY_CHANGED", changed),
                ),
            ]:
                add(
                    event,
                    "evt-v2-transition",
                    kind,
                    event.occurred_at,
                    [*filters, condition],
                    event.payload,
                )
    if not parts:
        raise HTTPException(404, "EVENT_NOT_FOUND")
    return (union_all(*parts) if len(parts) > 1 else parts[0]).subquery()


def reference(
    row: Mapping[Any, Any], pit: str, before: int, after: int, active: tuple[str, str] | None
) -> EventRecording:
    kind, at = row["kind"], utc(row["at"])
    data = row["payload"] if isinstance(row["payload"], dict) else {}
    common = {
        "eventId": row["event_id"],
        "pitId": pit,
        "eventType": kind,
        "occurredAt": at,
        "simulated": data.get("simulated") if isinstance(data.get("simulated"), bool) else None,
    }
    if kind == "COLLECTION":
        valid = all(isinstance(data.get(k), str) for k in ("detectionId", "scopeToken"))
        ended = utc(row["ended"]) if row["ended"] is not None and valid else None
        state = (
            "CLOSED"
            if ended is not None
            else "OPEN"
            if valid and active == (data["detectionId"], data["scopeToken"])
            else "UNKNOWN"
        )
        return EventRecording(
            **common,
            category="collection",
            title="수거 구간",
            eventTo=ended,
            windowKind="COLLECTION_CYCLE",
            windowState=state,
            windowFrom=at,
            windowTo=ended,
            reasonCodes=[]
            if state != "UNKNOWN"
            else ["COLLECTION_END_NOT_RECORDED" if valid else "INVALID_COLLECTION_METADATA"],
        )
    category: ReferenceCategory = (
        "alert" if kind == "ALERT" else "collection" if kind == "MANUAL_COLLECTION" else "error"
    )
    title = {
        "ALERT": "알림 이벤트",
        "MANUAL_COLLECTION": "수거 완료 확인",
        "LEGACY_ERROR": "센서 데이터 이상",
    }.get(kind, "측정 상태 변화")
    state_at, attribution, reasons = None, "RECORDED", []
    if kind in ("DEVICE_STATE_CHANGED", "MEASUREMENT_VALIDITY_CHANGED"):
        recorded = error_transition_event(
            EdgeV2Transition(
                id=row["number"], site_id="", edge_id="", kind=kind, occurred_at=at, payload=data
            )
        )
        if recorded is not None:
            title, state_at, attribution, reasons = (
                recorded.title,
                recorded.stateAtOccurrence,
                recorded.pitAttribution or "RECORDED",
                recorded.reasonCodes,
            )
        else:
            reasons = ["INVALID_EVENT_METADATA"]
    return EventRecording(
        **common,
        category=category,
        title=title,
        eventTo=at,
        windowKind="EVENT_CONTEXT",
        windowState="POINT",
        windowFrom=shifted(at, -before),
        windowTo=shifted(at, after),
        stateAtOccurrence=state_at,
        pitAttribution=attribution,
        reasonCodes=reasons,
    )
