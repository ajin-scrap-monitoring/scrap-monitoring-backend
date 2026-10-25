"""Queue fresh v2 event identities outside sensor ACKs; never backfill camera footage."""

from datetime import datetime, timedelta

from sqlalchemy import String, case, cast, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.edge_v2_operations import EdgeV2AlertLink
from src.app.models.edge_v2_projection import EdgeV2Latest, EdgeV2Transition
from src.app.models.log import AlertLog
from src.app.models.media_job import MediaClipJob
from src.app.schemas.history import EventCategory
from src.app.services.edge_v2_service import digest, utc
from src.app.services.error_history_service import error_transition_event

CURSOR_KIND = "media_v2_events"
AUTO_CATEGORIES = {"v2_alert": "alert", "v2_collection": "collection", "v2_error": "error"}
KINDS = (
    "ALERT_CREATED",
    "COLLECTION_DETECTED",
    "DEVICE_STATE_CHANGED",
    "MEASUREMENT_VALIDITY_CHANGED",
)


def clip_context() -> tuple[str, str, str] | None:
    settings = get_settings()
    source = next(
        (s for s in settings.EDGE_V2_MONITORING_SOURCES if s.pit_id == settings.MEDIA_PIT_ID), None
    )
    if not settings.MEDIA_SERVICE_URL or source is None:
        return None
    return (
        source.site_id,
        source.edge_id,
        digest([settings.MEDIA_SERVICE_URL, settings.MEDIA_CAMERA_ID, source.model_dump()]),
    )


def recording_category(category: str) -> EventCategory:
    if category in ("error", "v2_error"):
        return "error"
    return "alert" if category in ("alert", "v2_alert") else "collection"


def automatic_request_block(job: MediaClipJob, now: datetime) -> str | None:
    """Accepted clips remain pollable; this guard only controls a first remote creation."""
    settings = get_settings()
    context = clip_context()
    if (
        context is None
        or job.pit_id != settings.MEDIA_PIT_ID
        or job.camera_id != settings.MEDIA_CAMERA_ID
        or (job.snapshot or {}).get("_v2Context") != context[2]
    ):
        return "MEDIA_CLIP_SOURCE_CHANGED"
    if not settings.MEDIA_V2_EVENT_CLIPS_ENABLED:
        return "V2_EVENT_CLIPS_DISABLED"
    age = now - utc(job.occurred_at)
    if not timedelta(0) <= age <= timedelta(seconds=settings.MEDIA_V2_EVENT_CLIP_MAX_DELAY_SECONDS):
        return "EVENT_CLIP_WINDOW_EXPIRED"
    return None


async def queue_recent_events(db: AsyncSession, session_token: str, now: datetime) -> None:
    """Persist a bounded cursor and outbox rows together, without upstream I/O."""
    from src.app.services.media_job_service import enqueue_event_clip

    settings = get_settings()
    context = clip_context()
    if not settings.MEDIA_V2_EVENT_CLIPS_ENABLED or context is None:
        return
    site, edge, scope = context
    cursor = await db.get(EdgeV2Latest, (site, edge, CURSOR_KIND), populate_existing=True)
    event = EdgeV2Transition
    query = select(event).where(event.site_id == site, event.edge_id == edge, event.kind.in_(KINDS))
    token = digest([session_token, scope])
    if cursor is None or cursor.payload.get("sessionToken") != token:
        # A new process, assignment, upstream or enable cycle starts from the present.
        latest = (
            await db.scalar(
                select(func.max(event.id)).where(
                    event.site_id == site, event.edge_id == edge, event.kind.in_(KINDS)
                )
            )
            or 0
        )
        if cursor is None:
            cursor = EdgeV2Latest(site_id=site, edge_id=edge, kind=CURSOR_KIND)
            db.add(cursor)
        cursor.message_id, cursor.generated_at = token, now
        cursor.payload = {"sessionToken": token, "afterId": latest}
        await db.commit()
        return
    after = cursor.payload.get("afterId")
    if not isinstance(after, int) or isinstance(after, bool):
        raise ValueError("Invalid media event cursor")
    identifier_sql = case(
        (
            event.kind == "ALERT_CREATED",
            literal("evt-alt-") + cast(event.payload["alertId"].as_string(), String),
        ),
        (
            event.kind == "COLLECTION_DETECTED",
            literal("evt-auto-col-start-") + cast(event.id, String),
        ),
        else_=literal("evt-v2-transition-") + cast(event.id, String),
    )
    already_queued = exists(
        select(MediaClipJob.id).where(
            MediaClipJob.camera_id == settings.MEDIA_CAMERA_ID,
            MediaClipJob.event_id == identifier_sql,
        )
    )
    # IDs are allocated before commit, so a smaller ID may become visible later.
    # Keep the activation baseline fixed and revisit the small fresh window instead
    # of advancing past a concurrently uncommitted event. The outbox deduplicates it.
    earliest = max(
        utc(cursor.generated_at),
        now - timedelta(seconds=settings.MEDIA_V2_EVENT_CLIP_MAX_DELAY_SECONDS),
    )
    rows = (
        await db.scalars(
            query.where(
                event.id > after,
                event.occurred_at >= earliest,
                event.occurred_at <= now,
                event.payload["pitId"].as_string() == settings.MEDIA_PIT_ID,
                ~already_queued,
            )
            .order_by(event.id)
            .limit(1000)
        )
    ).all()
    queued = 0
    for row in rows:
        age = now - utc(row.occurred_at)
        if row.payload.get("pitId") != settings.MEDIA_PIT_ID or not (
            timedelta(0) <= age <= timedelta(seconds=settings.MEDIA_V2_EVENT_CLIP_MAX_DELAY_SECONDS)
        ):
            continue
        category, identifier = "", ""
        if row.kind == "ALERT_CREATED":
            alert_id = row.payload.get("alertId")
            if not isinstance(alert_id, int) or isinstance(alert_id, bool):
                continue
            linked = await db.scalar(
                select(AlertLog.id)
                .join(EdgeV2AlertLink, EdgeV2AlertLink.alert_id == AlertLog.id)
                .where(
                    AlertLog.id == alert_id,
                    AlertLog.pit_id == settings.MEDIA_PIT_ID,
                    AlertLog.triggered_at == row.occurred_at,
                    EdgeV2AlertLink.pit_id == settings.MEDIA_PIT_ID,
                    EdgeV2AlertLink.site_id == site,
                    EdgeV2AlertLink.edge_id == edge,
                )
            )
            if linked is not None:
                category, identifier = "v2_alert", f"evt-alt-{linked}"
        elif row.kind == "COLLECTION_DETECTED":
            if row.payload.get("confirmedCompletion") is False and all(
                isinstance(row.payload.get(key), str) for key in ("detectionId", "scopeToken")
            ):
                category, identifier = "v2_collection", f"evt-auto-col-start-{row.id}"
        else:
            recorded = error_transition_event(row)
            if recorded is not None and recorded.stateAtOccurrence == "UNAVAILABLE":
                category, identifier = "v2_error", recorded.id
        if identifier:
            job = await enqueue_event_clip(
                db, identifier, settings.MEDIA_PIT_ID, category, utc(row.occurred_at)
            )
            if job and job.category == category and job.clip_id is None and job.snapshot is None:
                # Only a hash is retained; no media address/token is stored in the outbox.
                job.snapshot = {
                    "_v2Context": scope,
                    "_v2Simulated": row.payload.get("simulated")
                    if isinstance(row.payload.get("simulated"), bool)
                    else None,
                }
                if category == "v2_collection":
                    job.snapshot = {
                        **job.snapshot,
                        "_v2Collection": {
                            "siteId": site,
                            "edgeId": edge,
                            "detectionId": row.payload["detectionId"],
                            "scopeToken": row.payload["scopeToken"],
                        },
                    }
                queued += 1
                if queued == 20:
                    break
    if queued:
        await db.commit()


async def collection_end(db: AsyncSession, job: MediaClipJob, now: datetime) -> datetime | None:
    """Pair recorded ends to the exact start; a missing end remains unknown."""
    data = (job.snapshot or {}).get("_v2Collection")
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) for key in ("siteId", "edgeId", "detectionId", "scopeToken")
    ):
        return None
    event = EdgeV2Transition
    ended = await db.scalar(
        select(event.occurred_at)
        .where(
            event.site_id == data["siteId"],
            event.edge_id == data["edgeId"],
            event.kind == "COLLECTION_SETTLED",
            event.payload["pitId"].as_string() == job.pit_id,
            event.payload["detectionId"].as_string() == data["detectionId"],
            event.payload["scopeToken"].as_string() == data["scopeToken"],
            event.occurred_at >= job.occurred_at,
            event.occurred_at <= now,
        )
        .order_by(event.occurred_at, event.id)
        .limit(1)
    )
    return utc(ended) if ended is not None else None
