"""Service for managing alert settings, recipients, and test notifications."""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.models.config import PitConfig
from src.app.models.log import AlertLog
from src.app.models.recipient import NotificationRecipientModel
from src.app.schemas.admin import (
    AlertSettings,
    AlertSettingsUpdate,
    ConfigurableAlertEventType,
    NotificationChannel,
    NotificationRecipient,
    NotificationRecipientCreate,
    NotificationRecipientPage,
    NotificationRecipientPatch,
    SubscriptionMode,
    TestNotificationRequest,
)
from src.app.schemas.history import PageMetadata
from src.app.schemas.monitoring import PreCollectionAlert

logger = logging.getLogger(__name__)


def _generate_alert_etag(pit_cfg: PitConfig) -> str:
    """Generate consistent ETag for alert settings from configuration state."""
    content = (
        f"{pit_cfg.id}:{pit_cfg.critical_threshold_percent}:"
        f"{pit_cfg.warning_threshold_percent}:{pit_cfg.updated_at.isoformat()}"
    )
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f'"{pit_cfg.id}-{digest}"'


class AdminService:
    """Handles admin settings, recipients management, and mock notification dispatch."""

    async def get_alert_settings(
        self,
        pit_id: str,
        db: AsyncSession,
    ) -> tuple[AlertSettings, str]:
        """Fetch alert settings and generate ETag version."""
        settings = get_settings()
        cfg_stmt = select(PitConfig).where(PitConfig.id == pit_id)
        res = await db.execute(cfg_stmt)
        pit_cfg = res.scalar_one_or_none()

        if not pit_cfg:
            pit_cfg = PitConfig(
                id=pit_id,
                name=f"스크랩 구덩이 {pit_id}",
                total_depth_cm=300.0,
                warning_threshold_percent=settings.DEFAULT_WARNING_THRESHOLD,
                critical_threshold_percent=settings.DEFAULT_CRITICAL_THRESHOLD,
            )
            db.add(pit_cfg)
            await db.commit()
            await db.refresh(pit_cfg)

        etag = _generate_alert_etag(pit_cfg)

        alert_settings = AlertSettings(
            version=etag,
            collectionThresholdPercent=pit_cfg.critical_threshold_percent,
            preCollectionAlert=PreCollectionAlert(
                enabled=True,
                thresholdPercent=pit_cfg.warning_threshold_percent,
            ),
            sendDelayMinutes=0,
            repeatIntervalMinutes=30,
            maximumRepeatCount=3,
            recoveryNotificationEnabled=True,
            eventTypes=["collection_required", "measurement_error", "device_error"],
        )
        return alert_settings, etag

    async def replace_alert_settings(
        self,
        pit_id: str,
        payload: AlertSettingsUpdate,
        db: AsyncSession,
    ) -> tuple[AlertSettings, str]:
        """Replace alert settings and return updated model with new ETag."""
        cfg_stmt = select(PitConfig).where(PitConfig.id == pit_id)
        res = await db.execute(cfg_stmt)
        pit_cfg = res.scalar_one_or_none()

        if not pit_cfg:
            pit_cfg = PitConfig(id=pit_id, name=f"스크랩 구덩이 {pit_id}")
            db.add(pit_cfg)

        pit_cfg.critical_threshold_percent = payload.collectionThresholdPercent
        pit_cfg.warning_threshold_percent = payload.preCollectionAlert.thresholdPercent
        pit_cfg.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(pit_cfg)

        etag = _generate_alert_etag(pit_cfg)

        updated = AlertSettings(
            version=etag,
            collectionThresholdPercent=pit_cfg.critical_threshold_percent,
            preCollectionAlert=PreCollectionAlert(
                enabled=True,
                thresholdPercent=pit_cfg.warning_threshold_percent,
            ),
            sendDelayMinutes=payload.sendDelayMinutes,
            repeatIntervalMinutes=payload.repeatIntervalMinutes,
            maximumRepeatCount=payload.maximumRepeatCount,
            recoveryNotificationEnabled=payload.recoveryNotificationEnabled,
            eventTypes=payload.eventTypes,
        )
        return updated, etag

    async def list_recipients(
        self,
        page: int,
        pageSize: int,
        db: AsyncSession,
    ) -> NotificationRecipientPage:
        """List paginated notification recipients ordered by name."""
        stmt = select(NotificationRecipientModel).order_by(NotificationRecipientModel.name.asc())
        res = await db.execute(stmt)
        records = res.scalars().all()

        recipients: list[NotificationRecipient] = []
        for r in records:
            channels = [cast(NotificationChannel, c) for c in r.channels.split(",") if c]
            event_types = [
                cast(ConfigurableAlertEventType, et) for et in r.event_types.split(",") if et
            ]
            recipients.append(
                NotificationRecipient(
                    id=r.id,
                    version=f'"{r.version_id}"',
                    name=r.name,
                    team=r.team,
                    email=r.email,
                    phone=r.phone,
                    enabled=r.enabled,
                    channels=channels,
                    subscriptionMode=cast(SubscriptionMode, r.subscription_mode),
                    eventTypes=event_types,
                )
            )

        total_items = len(recipients)
        total_pages = (total_items + pageSize - 1) // pageSize if total_items > 0 else 0
        start_idx = (page - 1) * pageSize
        end_idx = start_idx + pageSize

        return NotificationRecipientPage(
            items=recipients[start_idx:end_idx],
            page=PageMetadata(
                page=page,
                pageSize=pageSize,
                totalItems=total_items,
                totalPages=total_pages,
            ),
        )

    async def create_recipient(
        self,
        payload: NotificationRecipientCreate,
        db: AsyncSession,
    ) -> tuple[NotificationRecipient, str]:
        """Create new notification recipient."""
        # Check email uniqueness
        chk_stmt = select(NotificationRecipientModel).where(
            NotificationRecipientModel.email == str(payload.email)
        )
        chk_res = await db.execute(chk_stmt)
        if chk_res.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Recipient with email '{payload.email}' already exists",
            )

        new_id = f"recipient-{uuid.uuid4().hex[:8]}"
        rec_model = NotificationRecipientModel(
            id=new_id,
            version_id=1,
            name=payload.name,
            team=payload.team,
            email=str(payload.email),
            phone=payload.phone,
            enabled=payload.enabled,
            channels=",".join(payload.channels),
            subscription_mode=payload.subscriptionMode,
            event_types=",".join(payload.eventTypes),
        )
        db.add(rec_model)
        await db.commit()
        await db.refresh(rec_model)

        etag = f'"{rec_model.version_id}"'
        item = NotificationRecipient(
            id=rec_model.id,
            version=etag,
            name=rec_model.name,
            team=rec_model.team,
            email=rec_model.email,
            phone=rec_model.phone,
            enabled=rec_model.enabled,
            channels=payload.channels,
            subscriptionMode=payload.subscriptionMode,
            eventTypes=payload.eventTypes,
        )
        return item, etag

    async def update_recipient(
        self,
        recipient_id: str,
        payload: NotificationRecipientPatch,
        db: AsyncSession,
    ) -> tuple[NotificationRecipient, str] | None:
        """Partially update an existing recipient."""
        stmt = select(NotificationRecipientModel).where(
            NotificationRecipientModel.id == recipient_id
        )
        res = await db.execute(stmt)
        rec_model = res.scalar_one_or_none()
        if not rec_model:
            return None

        if payload.email and str(payload.email) != rec_model.email:
            chk_stmt = select(NotificationRecipientModel).where(
                NotificationRecipientModel.email == str(payload.email)
            )
            chk_res = await db.execute(chk_stmt)
            if chk_res.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Email '{payload.email}' is already in use by another recipient",
                )
            rec_model.email = str(payload.email)

        if payload.name is not None:
            rec_model.name = payload.name
        if payload.team is not None:
            rec_model.team = payload.team
        if payload.phone is not None:
            rec_model.phone = payload.phone
        if payload.enabled is not None:
            rec_model.enabled = payload.enabled
        if payload.channels is not None:
            rec_model.channels = ",".join(payload.channels)
        if payload.subscriptionMode is not None:
            rec_model.subscription_mode = payload.subscriptionMode
        if payload.eventTypes is not None:
            rec_model.event_types = ",".join(payload.eventTypes)

        rec_model.version_id += 1
        await db.commit()
        await db.refresh(rec_model)

        etag = f'"{rec_model.version_id}"'
        channels = [cast(NotificationChannel, c) for c in rec_model.channels.split(",") if c]
        event_types = [
            cast(ConfigurableAlertEventType, et) for et in rec_model.event_types.split(",") if et
        ]

        item = NotificationRecipient(
            id=rec_model.id,
            version=etag,
            name=rec_model.name,
            team=rec_model.team,
            email=rec_model.email,
            phone=rec_model.phone,
            enabled=rec_model.enabled,
            channels=channels,
            subscriptionMode=cast(SubscriptionMode, rec_model.subscription_mode),
            eventTypes=event_types,
        )
        return item, etag

    async def send_test_notification(
        self,
        payload: TestNotificationRequest,
        db: AsyncSession,
    ) -> None:
        """Dispatch mock test alert and log to AlertLog database."""
        stmt = select(NotificationRecipientModel).where(
            NotificationRecipientModel.id == payload.recipientId
        )
        res = await db.execute(stmt)
        rec_model = res.scalar_one_or_none()
        if not rec_model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipient '{payload.recipientId}' not found",
            )

        recipient_name = rec_model.name
        channels_str = ",".join(payload.channels).upper()

        logger.info(
            "[TEST NOTIFICATION DISPATCH] Target: %s, Channels: %s",
            recipient_name,
            channels_str,
        )

        # Record test alert log
        msg = (
            f"[테스트 알림] {recipient_name} 담당자에게 {channels_str} 채널로 테스트 알림 전송 완료"
        )
        test_log = AlertLog(
            pit_id="pit-01",
            triggered_at=datetime.now(UTC),
            fill_ratio_percent=0.0,
            channel=channels_str,
            status="SENT",
            message=msg,
        )
        db.add(test_log)
        await db.commit()


admin_service = AdminService()
