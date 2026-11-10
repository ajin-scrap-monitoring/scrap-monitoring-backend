"""Durable mail delivery isolated from receiver ACKs and operational evaluation."""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.app.core.config import get_settings
from src.app.db.session import get_sessionmaker
from src.app.models.alert_policy import AlertPolicy
from src.app.models.email_delivery import EmailDelivery, EmailEpisode
from src.app.models.recipient import NotificationRecipientModel as Recipient
from src.app.schemas.active_alerts_v2 import (
    ActiveFillAlertPageV2,
    ActiveFillAlertV2,
    ActiveHealthAlertV2,
)
from src.app.schemas.monitoring_v2 import MonitoringSnapshotV2
from src.app.services.active_alerts_v2_service import active_snapshot_alerts
from src.app.services.active_health_service import signature
from src.app.services.camera_status_service import CameraStatusService
from src.app.services.edge_v2_service import utc
from src.app.services.gmail_sender import GmailError, GmailSender
from src.app.services.monitoring_v2_service import build_snapshot

logger = logging.getLogger(__name__)
Alert = ActiveFillAlertV2 | ActiveHealthAlertV2


def address_digest(address: str) -> str:
    return hashlib.sha256(address.strip().casefold().encode()).hexdigest()


@dataclass(frozen=True)
class MailPolicy:
    types: tuple[str, ...] = ("collection_required", "measurement_error", "device_error")
    pre_alert: bool = True
    delay: int = 0
    interval: int = 30
    maximum: int = 3
    recovery: bool = True


async def policy_for(db: AsyncSession, pit: str) -> MailPolicy:
    row = await db.get(AlertPolicy, pit, populate_existing=True)
    if row is None:
        return MailPolicy()
    return MailPolicy(
        tuple(row.event_types),
        row.pre_alert_enabled,
        row.send_delay_minutes,
        row.repeat_interval_minutes,
        row.maximum_repeat_count,
        row.recovery_notification_enabled,
    )


def eligible(recipient: Recipient, event_type: str, policy: MailPolicy) -> bool:
    kind = "collection_required" if event_type == "pre_collection_alert" else event_type
    return (
        recipient.enabled
        and "email" in recipient.channels.split(",")
        and kind in policy.types
        and (event_type != "pre_collection_alert" or policy.pre_alert)
        and (recipient.subscription_mode == "global" or kind in recipient.event_types.split(","))
    )


async def cancel_pending(db: AsyncSession, episode: EmailEpisode, now: datetime) -> None:
    await db.execute(
        update(EmailDelivery)
        .where(
            EmailDelivery.episode_id == episode.id,
            EmailDelivery.status == "QUEUED",
            EmailDelivery.phase == "ALERT",
        )
        .values(status="CANCELLED", completed_at=now, error_code="EMAIL_CONDITION_CHANGED")
    )


async def ensure_delivery(
    db: AsyncSession,
    episode: EmailEpisode,
    phase: str,
    sequence: int,
    due: datetime,
    subject: str,
    body: str,
) -> None:
    existing = await db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.episode_id == episode.id,
            EmailDelivery.phase == phase,
            EmailDelivery.sequence == sequence,
        )
    )
    if existing is None:
        db.add(
            EmailDelivery(
                id=str(uuid4()),
                episode_id=episode.id,
                recipient_id=episode.recipient_id,
                address_digest=episode.address_digest,
                pit_id=episode.pit_id,
                event_type=episode.event_type,
                phase=phase,
                sequence=sequence,
                status="QUEUED",
                due_at=due,
                attempts=0,
                subject=subject,
                body=body,
            )
        )
    elif (existing.status == "QUEUED" and existing.attempts == 0) or (
        existing.status == "CANCELLED"
        and existing.error_code == "EMAIL_CONDITION_CHANGED"
        and phase == "ALERT"
        and episode.phase == "ACTIVE"
    ):
        existing.status, existing.error_code = "QUEUED", None
        existing.completed_at = None
        existing.due_at = due
        existing.subject, existing.body = subject, body


def known_clear(episode: EmailEpisode, page: ActiveFillAlertPageV2) -> bool:
    if episode.event_type == "collection_required":
        return page.evaluationState in ("NORMAL", "WARNING")
    if episode.event_type == "pre_collection_alert":
        return page.evaluationState == "NORMAL"
    return any(
        c.componentKind == episode.component_kind
        and c.componentId == episode.component_id
        and c.state == "CHECKED"
        for c in page.coverage
    )


async def synchronize_snapshot(
    db: AsyncSession,
    snapshot: MonitoringSnapshotV2,
    page: ActiveFillAlertPageV2,
    now: datetime,
) -> None:
    """Only current evaluated conditions are enrolled; historical logs are never replayed."""
    policy = await policy_for(db, page.pitId)
    recipients = {r.id: r for r in await db.scalars(select(Recipient))}
    rows = list(
        await db.scalars(
            select(EmailEpisode).where(
                EmailEpisode.pit_id == page.pitId,
                EmailEpisode.phase.in_(("ACTIVE", "UNKNOWN")),
            )
        )
    )
    fresh = (
        timedelta(0)
        <= now - page.computedAt
        < timedelta(seconds=min(30, get_settings().EDGE_V2_ASSESSMENT_TTL_SECONDS))
    )
    observed: set[str] = set()
    for item in page.items if fresh else []:
        state = (
            item.state
            if isinstance(item, ActiveHealthAlertV2)
            else ("CRITICAL" if item.type == "collection_required" else "WARNING")
        )
        token = signature(snapshot, item.id, state)
        for recipient in recipients.values():
            if not eligible(recipient, item.type, policy):
                continue
            digest = address_digest(recipient.email)
            previous = next(
                (
                    r
                    for r in rows
                    if r.condition_id == item.id
                    and r.recipient_id == recipient.id
                    and r.phase in ("ACTIVE", "UNKNOWN")
                ),
                None,
            )
            if previous and (
                previous.condition_token != token or previous.address_digest != digest
            ):
                previous.phase = "CLOSED"
                await cancel_pending(db, previous, now)
                previous = None
            episode = previous
            if episode is None:
                # Use our first current observation, not an old historical notification time.
                episode = EmailEpisode(
                    id=str(uuid4()),
                    pit_id=page.pitId,
                    condition_id=item.id,
                    condition_token=token,
                    recipient_id=recipient.id,
                    address_digest=digest,
                    condition_state=state,
                    event_type=item.type,
                    phase="ACTIVE",
                    started_at=now,
                    last_observed_at=page.computedAt,
                    sent_count=0,
                    component_kind=item.componentKind
                    if isinstance(item, ActiveHealthAlertV2)
                    else None,
                    component_id=item.componentId
                    if isinstance(item, ActiveHealthAlertV2)
                    else None,
                )
                db.add(episode)
                await db.flush()
            observed.add(episode.id)
            if episode.phase == "UNKNOWN" and episode.sent_count == 0:
                episode.started_at = now
            episode.phase = "ACTIVE"
            episode.last_observed_at = page.computedAt
            if episode.sent_count >= policy.maximum:
                continue
            due = (
                utc(episode.started_at) + timedelta(minutes=policy.delay)
                if episode.last_sent_at is None
                else utc(episode.last_sent_at)
                + timedelta(
                    seconds=max(1800, get_settings().ALERT_COOLDOWN_SECONDS, policy.interval * 60)
                )
            )
            simulated = "[시뮬레이션] " if item.simulated is True else ""
            warning = isinstance(item, ActiveFillAlertV2) and item.type == "pre_collection_alert"
            title = f"[스크랩 모니터링] {item.title}" if warning else simulated + item.title
            decision_time = (
                "판정 시각(UTC+9): "
                + page.computedAt.astimezone(timezone(timedelta(hours=9))).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if warning
                else f"판정 시각(UTC): {page.computedAt.isoformat()}"
            )
            if isinstance(item, ActiveFillAlertV2):
                label = (
                    "단면 높이 비율"
                    if snapshot.summary.inputSource == "HEIGHT_SECTIONS"
                    else "적재율"
                )
                value = (
                    str(
                        Decimal(str(item.loadPercent)).quantize(
                            Decimal("0.01"), rounding=ROUND_DOWN
                        )
                    )
                    if warning
                    else str(item.loadPercent)
                )
                detail = f"{label}: {value}%\n"
            else:
                detail = (
                    f"대상: {item.componentKind}/{item.componentId}\n"
                    f"상태: {item.state}\n사유: {', '.join(item.reasonCodes)}\n"
                )
            await ensure_delivery(
                db,
                episode,
                "ALERT",
                episode.sent_count + 1,
                due,
                title,
                f"{title}\n피트: {page.pitId}\n" + detail + f"조건: {item.id}\n{decision_time}\n"
                "현재 판정에 따른 자동 알림입니다.",
            )
    for episode in rows:
        if episode.id in observed or episode.phase == "CLOSED":
            continue
        current_recipient = recipients.get(episode.recipient_id)
        await cancel_pending(db, episode, now)
        allowed = current_recipient is not None and eligible(
            current_recipient, episode.event_type, policy
        )
        unchanged = (
            current_recipient is not None
            and address_digest(current_recipient.email) == episode.address_digest
        )
        same_scope = episode.condition_token == signature(
            snapshot, episode.condition_id, episode.condition_state
        )
        clear = fresh and same_scope and known_clear(episode, page)
        continuous = now - utc(episode.last_observed_at) < timedelta(seconds=30)
        if clear and allowed and unchanged and episode.phase == "ACTIVE" and continuous:
            episode.phase = "RESOLVED"
            episode.last_observed_at = page.computedAt
            if policy.recovery and episode.sent_count:
                await ensure_delivery(
                    db,
                    episode,
                    "RECOVERY",
                    1,
                    now,
                    "알림 조건 해소 확인",
                    f"피트: {page.pitId}\n조건: {episode.condition_id}\n"
                    f"해소 판정 시각(UTC): {page.computedAt.isoformat()}\n"
                    "물리 장비 수리나 수거 작업 완료를 뜻하지 않습니다.",
                )
        elif not allowed or not unchanged or not same_scope or clear:
            episode.phase = "CLOSED"
        else:
            episode.phase = "UNKNOWN"


async def enqueue_test(db: AsyncSession, recipient: Recipient) -> str:
    """Record an explicit administrator test independently of automatic subscriptions."""
    identifier = str(uuid4())
    db.add(
        EmailDelivery(
            id=identifier,
            recipient_id=recipient.id,
            address_digest=address_digest(recipient.email),
            pit_id="pit-01",
            event_type="test",
            phase="TEST",
            sequence=1,
            status="QUEUED",
            due_at=datetime.now(UTC),
            attempts=0,
            subject="스크랩 모니터링 테스트 알림",
            body="관리자가 요청한 이메일 연결 테스트입니다. 실제 수거·장애 알림이 아닙니다.",
        )
    )
    return identifier


class EmailDeliveryWorker:
    """One application worker, like the existing operational and media workers."""

    def __init__(self, sender: GmailSender | None = None) -> None:
        self.sender = sender or GmailSender()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._camera: CameraStatusService | None = None

    def start(self, camera: CameraStatusService) -> None:
        if get_settings().GMAIL_EMAIL_ENABLED and self._task is None:
            self._camera = camera
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.sender.close()

    async def recover_interrupted(self, db: AsyncSession) -> None:
        await db.execute(
            update(EmailDelivery)
            .where(EmailDelivery.status == "SENDING")
            .values(
                status="UNCERTAIN",
                error_code="EMAIL_PROCESS_INTERRUPTED",
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()

    async def _allowed(self, db: AsyncSession, job: EmailDelivery, now: datetime) -> bool:
        recipient = await db.get(Recipient, job.recipient_id, populate_existing=True)
        if recipient is None:
            return False
        if address_digest(recipient.email) != job.address_digest:
            return False
        if job.phase == "TEST":
            return True
        if not recipient.enabled or "email" not in recipient.channels.split(","):
            return False
        if not get_settings().EDGE_V2_OPERATIONS_ENABLED:
            return False
        if not any(s.pit_id == job.pit_id for s in get_settings().EDGE_V2_MONITORING_SOURCES):
            return False
        policy = await policy_for(db, job.pit_id)
        episode = await db.get(EmailEpisode, job.episode_id, populate_existing=True)
        if episode is None or not eligible(recipient, job.event_type, policy):
            return False
        if not timedelta(0) <= now - utc(episode.last_observed_at) < timedelta(seconds=30):
            return False
        if job.phase == "RECOVERY":
            active = await db.scalar(
                select(EmailEpisode.id)
                .where(
                    EmailEpisode.pit_id == job.pit_id,
                    EmailEpisode.condition_id == episode.condition_id,
                    EmailEpisode.recipient_id == recipient.id,
                    EmailEpisode.phase == "ACTIVE",
                )
                .limit(1)
            )
            return policy.recovery and episode.phase == "RESOLVED" and active is None
        if episode.phase != "ACTIVE" or episode.sent_count >= policy.maximum:
            return False
        due = (
            utc(episode.started_at) + timedelta(minutes=policy.delay)
            if episode.last_sent_at is None
            else utc(episode.last_sent_at)
            + timedelta(
                seconds=max(1800, get_settings().ALERT_COOLDOWN_SECONDS, policy.interval * 60)
            )
        )
        # A new episode, source change, or restart must not bypass the minimum cooldown.
        previous_attempt = await db.scalar(
            select(func.max(func.coalesce(EmailDelivery.completed_at, EmailDelivery.attempted_at)))
            .join(EmailEpisode, EmailEpisode.id == EmailDelivery.episode_id)
            .where(
                EmailEpisode.pit_id == episode.pit_id,
                EmailEpisode.condition_id == episode.condition_id,
                EmailEpisode.recipient_id == episode.recipient_id,
                EmailDelivery.phase == "ALERT",
                EmailDelivery.status.in_(("SENT", "SENDING", "UNCERTAIN")),
            )
        )
        if previous_attempt is not None:
            due = max(
                due,
                utc(previous_attempt)
                + timedelta(seconds=max(1800, get_settings().ALERT_COOLDOWN_SECONDS)),
            )
        if now < due:
            job.due_at = due
            return False
        return True

    async def deliver_one(self, factory: async_sessionmaker[AsyncSession], now: datetime) -> bool:
        now = utc(now)
        if not get_settings().GMAIL_EMAIL_ENABLED:
            return False
        async with factory() as db:
            job = await db.scalar(
                select(EmailDelivery)
                .where(
                    EmailDelivery.status == "QUEUED",
                    EmailDelivery.due_at <= now,
                )
                .order_by(EmailDelivery.due_at, EmailDelivery.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return False
            if not await self._allowed(db, job, now):
                if utc(job.due_at) <= now:
                    job.status, job.completed_at = "CANCELLED", now
                    job.error_code = "EMAIL_CONDITION_CHANGED"
                await db.commit()
                return True
            since = now - timedelta(hours=24)
            used = (
                await db.scalar(
                    select(func.count())
                    .select_from(EmailDelivery)
                    .where(
                        EmailDelivery.attempted_at >= since,
                    )
                )
                or 0
            )
            outside_window = job.attempted_at is None or utc(job.attempted_at) < since
            if used >= get_settings().GMAIL_MAX_MESSAGES_24H and outside_window:
                oldest = await db.scalar(
                    select(func.min(EmailDelivery.attempted_at)).where(
                        EmailDelivery.attempted_at >= since,
                    )
                )
                job.due_at = (
                    utc(oldest) + timedelta(hours=24, seconds=1)
                    if oldest
                    else now + timedelta(hours=24)
                )
                job.error_code = "EMAIL_DAILY_LIMIT"
                await db.commit()
                return False
            recipient = await db.get(Recipient, job.recipient_id)
            assert recipient is not None
            target = recipient.email
            job.status, job.attempted_at = "SENDING", now
            job.attempts += 1
            identifier, subject, body = job.id, job.subject, job.body
            await db.commit()  # Release DB transaction before contacting Google.
        error = None
        provider_id = None
        started = time.monotonic()
        try:
            provider_id = await self.sender.send(target, subject, body, identifier)
        except GmailError as failure:
            error = failure
        except asyncio.CancelledError:
            # Durable SENDING is quarantined on restart, never blindly re-sent.
            raise
        except Exception:
            error = GmailError("EMAIL_DELIVERY_UNKNOWN", uncertain=True)
        completed = now + timedelta(seconds=max(0, time.monotonic() - started))
        async with factory() as db:
            current = await db.get(EmailDelivery, identifier)
            assert current is not None
            if error:
                current.error_code = error.code
                current.status = "UNCERTAIN" if error.uncertain else "FAILED"
                if error.retry_after is not None and current.attempts < 3:
                    current.status = "QUEUED"
                    current.due_at = completed + timedelta(seconds=error.retry_after)
                current.completed_at = completed if current.status != "QUEUED" else None
            else:
                current.status, current.completed_at = "SENT", completed
                current.provider_message_id, current.error_code = provider_id, None
                if current.episode_id and current.phase == "ALERT":
                    episode = await db.get(EmailEpisode, current.episode_id)
                    assert episode is not None
                    episode.sent_count += 1
                    episode.last_sent_at = completed
            await db.commit()
        return True

    async def run_once(self, factory: async_sessionmaker[AsyncSession]) -> None:
        if not get_settings().GMAIL_EMAIL_ENABLED:
            return
        async with self._lock:
            async with factory() as db:
                await self.recover_interrupted(db)
            if get_settings().EDGE_V2_OPERATIONS_ENABLED:
                for source in get_settings().EDGE_V2_MONITORING_SOURCES:
                    async with factory() as db:
                        now = datetime.now(UTC)
                        snapshot = await build_snapshot(db, source, now)
                        page = await active_snapshot_alerts(
                            db, snapshot, include_health=True, camera_status=self._camera
                        )
                        await synchronize_snapshot(db, snapshot, page, datetime.now(UTC))
                        await db.commit()
            # Bound each sweep; large recipient sets continue on the next sweep.
            for _ in range(10):
                if not await self.deliver_one(factory, datetime.now(UTC)):
                    break

    async def _run(self) -> None:
        factory = get_sessionmaker()
        while True:
            try:
                await self.run_once(factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                # SQL/HTTP exceptions can contain credentials, addresses or message bodies.
                logger.error("Email delivery processing failed; next sweep will recheck")
            await asyncio.sleep(get_settings().GMAIL_POLL_SECONDS)


email_delivery_worker = EmailDeliveryWorker()
