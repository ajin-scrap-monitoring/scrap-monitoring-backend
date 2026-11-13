"""Bounded raw-body expiration; durable identities, hashes and summaries are never deleted."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, exists, literal_column, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import Settings, get_settings
from src.app.db.migrations.raw_retention_v1 import retention_sql
from src.app.models.edge_v2 import EdgeV2Artifact, EdgeV2Message
from src.app.models.edge_v2_projection import EdgeV2Latest
from src.app.services.edge_v2_service import digest, utc


@dataclass(frozen=True)
class RetentionCursor:
    artifact: tuple[str, int] | None = None
    message: tuple[datetime, int] | None = None
    artifacts_done: bool = False
    messages_done: bool = False


@dataclass
class RetentionBatch:
    dry_run: bool
    days: int
    cutoff: str
    artifact_bodies: int = 0
    measurement_bodies: int = 0
    state_clouds: int = 0
    invalid_times_skipped: int = 0
    candidates_examined: int = 0
    exhausted: bool = False


def expiration_cutoff(now: datetime, days: int) -> datetime:
    """Retain at least the configured days; expire only completed UTC days."""
    if now.tzinfo is None or not 1 <= days <= 3650:
        raise ValueError("Retention requires an aware time and 1..3650 days")
    return (now.astimezone(UTC) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def artifact_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        return None
    return timestamp.astimezone(UTC)


async def protected_legacy_ids(db: AsyncSession, settings: Settings) -> set[int]:
    """Preserve configured devices' last original even before a latest projection exists."""
    protected: set[int] = set()
    for device in settings.EDGE_V2_DEVICES:
        for kind in ("lidar_measurement", "edge_assessment"):
            query = select(EdgeV2Message.id).where(
                EdgeV2Message.site_id == device.site_id,
                EdgeV2Message.edge_id == device.edge_id,
                EdgeV2Message.message_type == kind,
            )
            if kind == "edge_assessment":
                query = query.where(EdgeV2Message.payload["assessment_type"].as_string() == "STATE")
            identifier = await db.scalar(
                query.order_by(EdgeV2Message.generated_at.desc(), EdgeV2Message.id.desc()).limit(1)
            )
            if identifier is not None:
                protected.add(identifier)
    return protected


async def retention_batch(
    db: AsyncSession,
    cursor: RetentionCursor | None = None,
    *,
    apply: bool = False,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[RetentionBatch, RetentionCursor]:
    """Process at most one configured batch per table; caller owns commit/rollback.

    Dry run issues no writes and does not start jobs. Apply must be explicitly enabled.
    Cursors advance even past malformed timestamps, so one damaged row cannot starve
    later candidates; a new sweep retries skipped rows without discarding them.
    """
    settings = settings or get_settings()
    if apply and not (
        settings.EDGE_V2_RAW_RETENTION_ENABLED and settings.EDGE_V2_COMPACT_STORAGE_ENABLED
    ):
        raise ValueError("Raw expiration must be explicitly enabled with compact storage")
    current = now or datetime.now(UTC)
    cutoff = expiration_cutoff(current, settings.EDGE_V2_RAW_RETENTION_DAYS)
    cursor = cursor or RetentionCursor()
    result = RetentionBatch(
        dry_run=not apply,
        days=settings.EDGE_V2_RAW_RETENTION_DAYS,
        cutoff=cutoff.isoformat(),
    )
    dialect = db.get_bind().dialect.name
    sql = retention_sql(dialect)
    size = settings.EDGE_V2_RAW_RETENTION_BATCH_SIZE
    marker: dict[str, Any] = {
        "retention": "RAW_EXPIRED",
        "expired_at": current.astimezone(UTC).isoformat(),
    }

    if not cursor.artifacts_done:
        model = EdgeV2Artifact
        day = literal_column(sql.artifact_day, type_=String())
        timestamp = literal_column(sql.artifact_time, type_=String())
        query = (
            select(
                model.id, model.kind, model.content_hash, day.label("day"), timestamp.label("time")
            )
            .where(text(sql.artifact_where), day < cutoff.date().isoformat())
            .order_by(day, model.id)
            .limit(size)
        )
        if cursor.artifact is not None:
            last_day, last_id = cursor.artifact
            query = query.where(or_(day > last_day, and_(day == last_day, model.id > last_id)))
        if apply:
            query = query.with_for_update(skip_locked=True)
        rows = (await db.execute(query)).mappings().all()
        for row in rows:
            cursor = replace(cursor, artifact=(row["day"], row["id"]))
            result.candidates_examined += 1
            observed = artifact_time(row["time"])
            if observed is None or observed >= cutoff:
                result.invalid_times_skipped += 1
                continue
            cloud = row["kind"] == "assessment_revision"
            if cloud:
                result.state_clouds += 1
            else:
                result.artifact_bodies += 1
            if not apply:
                continue
            if not row["content_hash"] or cloud:
                original = await db.get(model, row["id"])
                assert original is not None
                if not original.content_hash:
                    original.content_hash = digest(original.payload)
                if cloud:
                    kept = dict(original.payload)
                    kept.pop("filtered_point_clouds", None)
                    original.payload = kept
                else:
                    original.payload = dict(marker)
            else:
                await db.execute(
                    update(model)
                    .where(model.id == row["id"])
                    .values(payload=dict(marker))
                    .execution_options(synchronize_session=False)
                )
        cursor = replace(cursor, artifacts_done=len(rows) < size)

    if not cursor.messages_done:
        message = EdgeV2Message
        current_snapshot = exists(
            select(1).where(
                EdgeV2Latest.site_id == message.site_id,
                EdgeV2Latest.edge_id == message.edge_id,
                EdgeV2Latest.message_id == message.message_id,
            )
        )
        query_messages = (
            select(message.id, message.message_type, message.received_at)
            .where(text(sql.message_where), message.received_at < cutoff, ~current_snapshot)
            .order_by(message.received_at, message.id)
            .limit(size)
        )
        protected = await protected_legacy_ids(db, settings)
        if protected:
            query_messages = query_messages.where(message.id.not_in(protected))
        if cursor.message is not None:
            last_time, last_id = cursor.message
            query_messages = query_messages.where(
                or_(
                    message.received_at > last_time,
                    and_(message.received_at == last_time, message.id > last_id),
                )
            )
        if apply:
            query_messages = query_messages.with_for_update(skip_locked=True)
        messages = (await db.execute(query_messages)).mappings().all()
        for row in messages:
            cursor = replace(cursor, message=(utc(row["received_at"]), row["id"]))
            result.candidates_examined += 1
            if row["message_type"] == "edge_assessment":
                result.state_clouds += 1
                if apply:
                    original_message = await db.get(message, row["id"])
                    assert original_message is not None
                    kept = dict(original_message.payload)
                    kept.pop("filtered_point_clouds", None)
                    original_message.payload = kept
            else:
                result.measurement_bodies += 1
                if apply:
                    await db.execute(
                        update(message)
                        .where(message.id == row["id"])
                        .values(payload=dict(marker))
                        .execution_options(synchronize_session=False)
                    )
        cursor = replace(cursor, messages_done=len(messages) < size)
    result.exhausted = cursor.artifacts_done and cursor.messages_done
    return result, cursor
