"""Atomic threshold/policy persistence and optimistic browser concurrency checks."""

import hashlib
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.schemas.admin import AlertSettings, AlertSettingsUpdate, ConfigurableAlertEventType
from src.app.schemas.monitoring import PreCollectionAlert


def _generate_alert_etag(pit_cfg: PitConfig, policy: AlertPolicy) -> str:
    """Generate consistent ETag for alert settings from configuration state."""
    timestamp = pit_cfg.updated_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    content = (
        f"{pit_cfg.id}:{pit_cfg.critical_threshold_percent}:"
        f"{pit_cfg.warning_threshold_percent}:{timestamp.astimezone(UTC).isoformat()}:"
        f"{policy.version_id}"
    )
    digest = hashlib.sha256(content.encode()).hexdigest()[:24]
    return f'"{pit_cfg.id}-{digest}"'


def snapshot(pit: PitConfig, policy: AlertPolicy) -> tuple[AlertSettings, str]:
    etag = _generate_alert_etag(pit, policy)
    return AlertSettings(
        version=etag,
        collectionThresholdPercent=pit.critical_threshold_percent,
        preCollectionAlert=PreCollectionAlert(
            enabled=policy.pre_alert_enabled, thresholdPercent=pit.warning_threshold_percent
        ),
        sendDelayMinutes=policy.send_delay_minutes,
        repeatIntervalMinutes=policy.repeat_interval_minutes,
        maximumRepeatCount=policy.maximum_repeat_count,
        recoveryNotificationEnabled=policy.recovery_notification_enabled,
        eventTypes=cast(list[ConfigurableAlertEventType], policy.event_types),
    ), etag


async def settings_rows(
    db: AsyncSession, pit_id: str, *, lock: bool = False
) -> tuple[PitConfig, AlertPolicy]:
    pit_query = (
        select(PitConfig).where(PitConfig.id == pit_id).execution_options(populate_existing=True)
    )
    policy_query = (
        select(AlertPolicy)
        .where(AlertPolicy.pit_id == pit_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        pit_query = pit_query.with_for_update()
        policy_query = policy_query.with_for_update()
    dialect = db.get_bind().dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise HTTPException(503, "Unsupported settings storage")
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    pit = await db.scalar(pit_query)
    if pit is None:
        if lock:
            raise HTTPException(412, "Retrieve the current settings before updating")
        settings = get_settings()
        # A concurrent first reader may have created the same pit.
        await db.execute(
            insert(PitConfig)
            .values(
                id=pit_id,
                name=f"스크랩 구덩이 {pit_id}",
                total_depth_cm=300.0,
                warning_threshold_percent=settings.DEFAULT_WARNING_THRESHOLD,
                critical_threshold_percent=settings.DEFAULT_CRITICAL_THRESHOLD,
            )
            .on_conflict_do_nothing(index_elements=[PitConfig.id])
        )
        pit = await db.scalar(pit_query)
        if pit is None:
            raise HTTPException(503, "Settings storage unavailable")
    policy = await db.scalar(policy_query)
    if policy is None:
        if lock:
            raise HTTPException(412, "Retrieve the current settings before updating")
        # The unique pit key makes lazy initialization safe to retry.
        await db.execute(
            insert(AlertPolicy)
            .values(pit_id=pit_id)
            .on_conflict_do_nothing(index_elements=[AlertPolicy.pit_id])
        )
        policy = await db.scalar(policy_query)
        if policy is None:
            raise HTTPException(503, "Settings storage unavailable")
    return pit, policy


async def get_settings_snapshot(db: AsyncSession, pit_id: str) -> tuple[AlertSettings, str]:
    try:
        pit, policy = await settings_rows(db, pit_id)
        result = snapshot(pit, policy)
        await db.commit()
        return result
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(503, "Settings storage unavailable") from None


async def replace_settings(
    db: AsyncSession, pit_id: str, payload: AlertSettingsUpdate, if_match: str
) -> tuple[AlertSettings, str]:
    try:
        pit, policy = await settings_rows(db, pit_id, lock=True)
        if if_match != _generate_alert_etag(pit, policy):
            raise HTTPException(412, "Precondition failed: ETag does not match current version")
        # Compare-and-update is necessary on SQLite where SELECT FOR UPDATE is
        # ignored, and also catches writers using the separate pit config API.
        policy_change = await db.execute(
            update(AlertPolicy)
            .where(AlertPolicy.pit_id == pit_id, AlertPolicy.version_id == policy.version_id)
            .values(
                pre_alert_enabled=payload.preCollectionAlert.enabled,
                send_delay_minutes=payload.sendDelayMinutes,
                repeat_interval_minutes=payload.repeatIntervalMinutes,
                maximum_repeat_count=payload.maximumRepeatCount,
                recovery_notification_enabled=payload.recoveryNotificationEnabled,
                event_types=list(payload.eventTypes),
                version_id=policy.version_id + 1,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        pit_change = await db.execute(
            update(PitConfig)
            .where(
                PitConfig.id == pit_id,
                PitConfig.updated_at == pit.updated_at,
                PitConfig.critical_threshold_percent == pit.critical_threshold_percent,
                PitConfig.warning_threshold_percent == pit.warning_threshold_percent,
            )
            .values(
                critical_threshold_percent=payload.collectionThresholdPercent,
                warning_threshold_percent=payload.preCollectionAlert.thresholdPercent,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(policy_change, "rowcount", 0) != 1 or getattr(pit_change, "rowcount", 0) != 1:
            raise HTTPException(412, "Precondition failed: settings changed concurrently")
        await db.refresh(pit)
        await db.refresh(policy)
        result = snapshot(pit, policy)
        await db.commit()
        return result
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(503, "Settings storage unavailable") from None
    except Exception:
        await db.rollback()
        raise
