"""Record a whitelisted change in the writer's transaction; never commit independently."""

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.config import PitConfig
from src.app.models.settings_history import SettingsHistory
from src.app.schemas.session import User
from src.app.schemas.settings_history import (
    SettingsActor,
    SettingsHistoryItem,
    SettingsHistoryPage,
    SettingsResource,
)


def pit_values(pit: PitConfig) -> dict[str, Any]:
    """Explicit business fields only: no arbitrary request bodies, credentials or clocks."""
    return {
        key: getattr(pit, key)
        for key in (
            "name",
            "total_depth_cm",
            "warning_threshold_percent",
            "critical_threshold_percent",
        )
    }


def record_change(
    db: AsyncSession,
    *,
    actor: User,
    pit_id: str,
    resource: SettingsResource,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    if actor.role != "administrator":
        raise HTTPException(403, "Administrator access required")
    fields = sorted(
        key for key, value in after.items() if before is None or before.get(key) != value
    )
    if not fields:
        return
    db.add(
        SettingsHistory(
            actor_username=actor.id,
            actor_display_name=actor.displayName,
            pit_id=pit_id,
            resource=resource,
            operation="create" if before is None else "update",
            before=before,
            after=after,
            changed_fields=fields,
        )
    )


async def history_page(
    db: AsyncSession,
    *,
    limit: int,
    cursor: int | None,
    pit_id: str | None,
    actor_id: str | None,
    resource: SettingsResource | None,
    from_time: datetime | None,
    to_time: datetime | None,
) -> SettingsHistoryPage:
    statement = select(SettingsHistory)
    for field, value in (
        (SettingsHistory.pit_id, pit_id),
        (SettingsHistory.actor_username, actor_id),
        (SettingsHistory.resource, resource),
    ):
        if value is not None:
            statement = statement.where(field == value)
    if cursor is not None:
        statement = statement.where(SettingsHistory.id < cursor)
    if from_time is not None:
        statement = statement.where(SettingsHistory.occurred_at >= from_time)
    if to_time is not None:
        statement = statement.where(SettingsHistory.occurred_at < to_time)
    try:
        rows = (
            await db.scalars(statement.order_by(SettingsHistory.id.desc()).limit(limit + 1))
        ).all()
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(503, "Settings history storage unavailable") from None
    items = [
        SettingsHistoryItem(
            id=str(row.id),
            occurredAt=row.occurred_at,
            actor=SettingsActor(id=row.actor_username, displayName=row.actor_display_name),
            pitId=row.pit_id,
            resource=row.resource,
            operation=row.operation,
            before=row.before,
            after=row.after,
            changedFields=row.changed_fields,
        )
        for row in rows[:limit]
    ]
    return SettingsHistoryPage(items=items, nextCursor=items[-1].id if len(rows) > limit else None)
