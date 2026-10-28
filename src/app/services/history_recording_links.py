"""Page-bounded, read-only links from history to shared recording event metadata."""

from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.app.core.config import get_settings
from src.app.models.edge_v2_projection import EdgeV2Transition
from src.app.schemas.history import Event
from src.app.services.event_recording_queries import event_table, playback_scope


async def recording_event_links(
    db: AsyncSession, pit_id: str, events: list[Event]
) -> dict[str, str]:
    """Resolve references without checking archives, creating clips or altering clip IDs.

    Use the recording query's source/pit filters and earliest matching collection end.
    Ambiguous starts or duplicate earliest ends cannot identify a unique cycle.
    """
    settings = get_settings()
    if (
        not events
        or not (settings.MEDIA_DAILY_SERVICE_URL or settings.MEDIA_SERVICE_URL)
        or pit_id != settings.MEDIA_PIT_ID
    ):
        return {}
    pit, source = playback_scope(pit_id)
    now = datetime.now(UTC)
    ordinary = {event.id for event in events if event.collectionSource != "HEIGHT_SETTLED"}
    links: dict[str, str] = {}
    if ordinary:
        table = event_table(
            db, pit, source, "all", None, None, now, 30, 30, None, only_ids=ordinary
        )
        identifiers = await db.scalars(select(table.c.event_id))
        links.update((identifier, identifier) for identifier in identifiers)

    end_ids = [
        int(event.id.removeprefix("evt-auto-col-"))
        for event in events
        if event.collectionSource == "HEIGHT_SETTLED"
    ]
    if source is None or not end_ids:
        return links
    # Reuse the playback query so a later or unrelated end cannot acquire a link.
    table = event_table(db, pit, source, "collection", None, None, now, 30, 30, None)
    end, other = aliased(EdgeV2Transition), aliased(EdgeV2Transition)
    duplicate_end = (
        select(other.id)
        .where(
            other.id != end.id,
            other.site_id == end.site_id,
            other.edge_id == end.edge_id,
            other.kind == "COLLECTION_SETTLED",
            other.occurred_at == end.occurred_at,
            other.payload["pitId"].as_string() == pit,
            other.payload["detectionId"].as_string() == end.payload["detectionId"].as_string(),
            other.payload["scopeToken"].as_string() == end.payload["scopeToken"].as_string(),
        )
        .correlate(end)
        .exists()
    )
    rows = await db.execute(
        select(end.id, func.min(table.c.event_id))
        .select_from(
            table.join(
                end,
                and_(
                    end.occurred_at == table.c.ended,
                    end.payload["detectionId"].as_string()
                    == table.c.payload["detectionId"].as_string(),
                    end.payload["scopeToken"].as_string()
                    == table.c.payload["scopeToken"].as_string(),
                ),
            )
        )
        .where(
            table.c.kind == "COLLECTION",
            end.id.in_(end_ids),
            end.site_id == source.site_id,
            end.edge_id == source.edge_id,
            end.kind == "COLLECTION_SETTLED",
            end.payload["pitId"].as_string() == pit,
            end.payload["detectionId"].as_string() != "",
            end.payload["scopeToken"].as_string() != "",
            ~duplicate_end,
        )
        .group_by(end.id)
        .having(func.count() == 1)
    )
    links.update((f"evt-auto-col-{identifier}", target) for identifier, target in rows)
    return links
