"""Durable delivery through current synthetic alarms and administrator requests."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_edge_v2_operations import operations_config as operations_config
from test_edge_v2_operations import tick
from test_monitoring_v2 import NOW, login

from src.app.core.config import get_settings
from src.app.models.alert_policy import AlertPolicy
from src.app.models.config import PitConfig
from src.app.models.email_delivery import EmailDelivery, EmailEpisode
from src.app.models.recipient import NotificationRecipientModel as Recipient
from src.app.services.active_alerts_v2_service import active_snapshot_alerts
from src.app.services.email_delivery_service import (
    EmailDeliveryWorker,
    enqueue_test,
    synchronize_snapshot,
)
from src.app.services.gmail_sender import GmailError


class SyntheticSender:
    def __init__(self, error=None):
        self.requests = []
        self.error = error

    async def send(self, address, subject, body, identifier):
        self.requests.append((address, subject, body, identifier))
        if self.error:
            raise self.error
        return "synthetic-provider-id"

    async def close(self):
        pass


@pytest.fixture
async def mail(test_engine, test_db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "GMAIL_EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "GMAIL_MAX_MESSAGES_24H", 450)
    db = test_db_session
    db.add(
        PitConfig(
            id="pit-demo",
            name="Synthetic pit",
            total_depth_cm=100,
            critical_threshold_percent=90,
            warning_threshold_percent=75,
        )
    )
    await db.flush()
    policy = AlertPolicy(pit_id="pit-demo")
    recipient = Recipient(
        id="synthetic-recipient",
        name="Synthetic",
        team="Test",
        email="receiver@example.test",
        channels="email",
        subscription_mode="global",
    )
    db.add_all([policy, recipient])
    await db.commit()
    return db, policy, recipient, async_sessionmaker(test_engine, expire_on_commit=False)


async def collect(client, db, ratio=0.95, seconds=0):
    snapshot = await tick(client, db, ratio, seconds)
    page = await active_snapshot_alerts(db, snapshot)
    await synchronize_snapshot(db, snapshot, page, NOW + timedelta(seconds=seconds))
    await db.commit()
    return snapshot, page


async def jobs(db):
    return list(await db.scalars(select(EmailDelivery).execution_options(populate_existing=True)))


async def test_current_alert_is_queued_once_and_delivered_outside_transaction(client, mail):
    db, _, _, factory = mail
    await collect(client, db)
    await collect(client, db, seconds=1)
    queued = await jobs(db)
    assert len(queued) == 1 and queued[0].status == "QUEUED"
    sender = SyntheticSender()
    assert await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=1))
    assert len(sender.requests) == 1
    delivered = (await jobs(db))[0]
    assert delivered.status == "SENT" and delivered.provider_message_id == "synthetic-provider-id"
    assert delivered.attempts == 1
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=2))


async def test_delay_rechecks_current_condition_and_cancels_after_clear(client, mail):
    db, policy, _, factory = mail
    policy.send_delay_minutes = 1
    await db.commit()
    await collect(client, db)
    sender = SyntheticSender()
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    await collect(client, db, ratio=0.5, seconds=10)
    assert (await jobs(db))[0].status == "CANCELLED"
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=61))
    assert sender.requests == []


@pytest.mark.parametrize("disabled", ["recipient", "channel", "policy", "subscription"])
async def test_delivery_rechecks_live_subscription_before_send(client, mail, disabled):
    db, policy, recipient, factory = mail
    await collect(client, db)
    if disabled == "recipient":
        recipient.enabled = False
    elif disabled == "channel":
        recipient.channels = "sms"
    elif disabled == "policy":
        policy.event_types = []
    else:
        recipient.subscription_mode = "custom"
        recipient.event_types = "device_error"
    await db.commit()
    sender = SyntheticSender()
    assert await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    assert (await jobs(db))[0].status == "CANCELLED"
    assert sender.requests == []


async def test_changed_email_is_not_used_for_an_old_delivery(client, mail):
    db, _, recipient, factory = mail
    await collect(client, db)
    recipient.email = "other@example.test"
    await db.commit()
    sender = SyntheticSender()
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    assert sender.requests == []
    assert (await jobs(db))[0].status == "CANCELLED"


async def test_repeat_interval_minimum_and_maximum_survive_new_workers(client, mail):
    db, policy, _, factory = mail
    policy.repeat_interval_minutes = 0
    policy.maximum_repeat_count = 2
    await db.commit()
    sender = SyntheticSender()
    await collect(client, db)
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    await collect(client, db, seconds=1)
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=1799))
    await collect(client, db, seconds=1801)
    assert await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=1801))
    await collect(client, db, seconds=3600)
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=3600))
    assert len(sender.requests) == 2


@pytest.mark.parametrize("recovery", [False, True])
async def test_recovery_requires_accepted_alert_and_known_clear(client, mail, recovery):
    db, policy, _, factory = mail
    policy.recovery_notification_enabled = recovery
    await db.commit()
    sender = SyntheticSender()
    await collect(client, db)
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    await collect(client, db, ratio=0.5, seconds=1)
    if recovery:
        assert await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=1))
        assert (await jobs(db))[-1].phase == "RECOVERY"
        assert "해소" in sender.requests[-1][1]
    else:
        assert not await EmailDeliveryWorker(sender).deliver_one(
            factory, NOW + timedelta(seconds=1)
        )
        assert len(sender.requests) == 1


async def test_unknown_snapshot_does_not_invent_recovery_or_retry_old_alert(client, mail):
    db, _, _, factory = mail
    snapshot, page = await collect(client, db)
    sender = SyntheticSender()
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    page.items = []
    page.evaluationState = "UNKNOWN"
    page.computedAt = NOW + timedelta(seconds=1)
    await synchronize_snapshot(db, snapshot, page, page.computedAt)
    await db.commit()
    assert (await db.scalar(select(EmailEpisode))).phase == "UNKNOWN"
    assert len(await jobs(db)) == 1
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, page.computedAt)


@pytest.mark.parametrize("outcome", ["uncertain", "interrupted", "rejected"])
async def test_uncertain_or_failed_delivery_is_not_blindly_resent(client, mail, outcome):
    db, _, _, factory = mail
    await collect(client, db)
    error = (
        GmailError("EMAIL_DELIVERY_UNKNOWN", uncertain=True)
        if outcome != "rejected"
        else GmailError("EMAIL_REJECTED")
    )
    sender = SyntheticSender(error)
    if outcome == "interrupted":
        await db.execute(update(EmailDelivery).values(status="SENDING", attempted_at=NOW))
        await db.commit()
        await EmailDeliveryWorker(sender).recover_interrupted(db)
    else:
        await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    assert (await jobs(db))[0].status == ("FAILED" if outcome == "rejected" else "UNCERTAIN")
    await collect(client, db, seconds=1)
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=1))
    assert len(sender.requests) == (0 if outcome == "interrupted" else 1)


async def test_explicit_rate_limit_is_bounded_and_preserves_delivery_id(mail):
    db, _, recipient, factory = mail
    identifier = await enqueue_test(db, recipient)
    await db.commit()
    job = await db.get(EmailDelivery, identifier)
    at = job.due_at
    sender = SyntheticSender(GmailError("EMAIL_RATE_LIMITED", retry_after=300))
    worker = EmailDeliveryWorker(sender)
    for attempt in range(3):
        assert await worker.deliver_one(factory, at + timedelta(seconds=attempt * 301))
    assert (await jobs(db))[0].status == "FAILED"
    assert len({r[3] for r in sender.requests}) == 1
    assert not await worker.deliver_one(factory, at + timedelta(hours=1))


async def test_rolling_budget_defers_without_network(mail, monkeypatch):
    db, _, recipient, factory = mail
    monkeypatch.setattr(get_settings(), "GMAIL_MAX_MESSAGES_24H", 1)
    identifier = await enqueue_test(db, recipient)
    await db.commit()
    at = (await db.get(EmailDelivery, identifier)).due_at
    sender = SyntheticSender()
    worker = EmailDeliveryWorker(sender)
    assert await worker.deliver_one(factory, at)
    second = await enqueue_test(db, recipient)
    await db.commit()
    assert not await worker.deliver_one(factory, at + timedelta(seconds=2))
    pending = await db.get(EmailDelivery, second, populate_existing=True)
    assert pending.status == "QUEUED" and pending.error_code == "EMAIL_DAILY_LIMIT"
    assert len(sender.requests) == 1


async def test_disabled_worker_does_not_send_pending_test(mail, monkeypatch):
    db, _, recipient, factory = mail
    await enqueue_test(db, recipient)
    await db.commit()
    monkeypatch.setattr(get_settings(), "GMAIL_EMAIL_ENABLED", False)
    sender = SyntheticSender()
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(days=365))
    assert sender.requests == []


async def test_retry_from_before_24_hour_window_cannot_bypass_budget(mail, monkeypatch):
    db, _, recipient, factory = mail
    monkeypatch.setattr(get_settings(), "GMAIL_MAX_MESSAGES_24H", 1)
    first = await enqueue_test(db, recipient)
    second = await enqueue_test(db, recipient)
    await db.commit()
    current = await db.get(EmailDelivery, first)
    now = current.due_at
    current.status, current.attempted_at = "SENT", now
    old = await db.get(EmailDelivery, second)
    old.attempted_at, old.attempts = now - timedelta(days=2), 1
    old.due_at = now
    await db.commit()
    sender = SyntheticSender()
    assert not await EmailDeliveryWorker(sender).deliver_one(factory, now)
    assert sender.requests == []
    pending = await db.get(EmailDelivery, second, populate_existing=True)
    assert pending.status == "QUEUED" and pending.error_code == "EMAIL_DAILY_LIMIT"


async def test_admin_test_is_durable_and_receipt_is_admin_only(client, mail):
    db, _, recipient, _ = mail
    recipient_id = recipient.id
    await login(client)
    # Retrieve a CSRF token from the real authentication endpoint.
    response = await client.post(
        "/api/v1/session", json={"username": "admin", "password": "admin1234"}
    )
    token = response.json()["csrfToken"]
    requested = await client.post(
        "/api/v1/notifications/test",
        headers={"X-CSRF-Token": token},
        json={"recipientId": recipient_id, "channels": ["email", "sms"]},
    )
    assert requested.status_code == 202
    identifier = requested.json()["emailDeliveryId"]
    receipt = await client.get(f"/api/v1/notifications/email-deliveries/{identifier}")
    assert receipt.status_code == 200
    assert receipt.json()["status"] == "QUEUED"
    assert "body" not in receipt.json() and "address" not in receipt.json()
    await client.post("/api/v1/session", json={"username": "operator", "password": "operator1234"})
    assert (
        await client.get(f"/api/v1/notifications/email-deliveries/{identifier}")
    ).status_code == 403
    assert await db.scalar(select(func.count()).select_from(EmailDelivery)) == 1


async def test_new_episode_cannot_bypass_same_condition_cooldown(client, mail):
    db, _, _, factory = mail
    sender = SyntheticSender()
    await collect(client, db)
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    await collect(client, db, ratio=0.5, seconds=1)
    await collect(client, db, seconds=2)
    # Recovery from the earlier episode is suppressed when the alarm recurs.
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=2))
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW + timedelta(seconds=2))
    assert len(sender.requests) == 1
    alerts = [j for j in await jobs(db) if j.phase == "ALERT" and j.status == "QUEUED"]
    assert len(alerts) == 1
    assert alerts[0].due_at.replace(tzinfo=NOW.tzinfo) > NOW + timedelta(seconds=1800)


async def test_source_change_does_not_manufacture_recovery(client, mail):
    db, _, _, factory = mail
    sender = SyntheticSender()
    snapshot, page = await collect(client, db)
    await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    # The new source is healthy, but it cannot resolve the previous source's evidence.
    snapshot.source = snapshot.source.model_copy(update={"site_id": "other-synthetic-site"})
    page.items = []
    page.evaluationState = "NORMAL"
    page.computedAt = NOW + timedelta(seconds=1)
    await synchronize_snapshot(db, snapshot, page, page.computedAt)
    await db.commit()
    assert all(j.phase != "RECOVERY" for j in await jobs(db))


@pytest.mark.parametrize(
    "channel,custom,enabled", [("sms", False, True), ("email", True, True), ("email", False, False)]
)
async def test_ineligible_recipient_has_no_automatic_job(client, mail, channel, custom, enabled):
    db, _, recipient, _ = mail
    recipient.channels, recipient.enabled = channel, enabled
    if custom:
        recipient.subscription_mode, recipient.event_types = "custom", "device_error"
    await db.commit()
    await collect(client, db)
    assert await jobs(db) == []


async def test_warning_switch_controls_automatic_mail(client, mail):
    db, policy, _, _ = mail
    policy.pre_alert_enabled = False
    await db.commit()
    await collect(client, db, ratio=0.8)
    assert await jobs(db) == []
    policy.pre_alert_enabled = True
    await db.commit()
    await collect(client, db, ratio=0.8, seconds=1)
    assert (await jobs(db))[0].event_type == "pre_collection_alert"


async def test_health_condition_and_known_recovery_share_existing_evaluation(client, mail):
    db, policy, _, factory = mail
    policy.event_types = ["device_error"]
    await db.commit()
    snapshot = await tick(client, db, 0.5, 0)
    sensor = snapshot.deviceStatus.snapshot.sensors[0]
    sensor.data_state = "UNAVAILABLE"
    sensor.reason_codes = ["SYNTHETIC_DEVICE_UNAVAILABLE"]
    page = await active_snapshot_alerts(db, snapshot, include_health=True)
    await synchronize_snapshot(db, snapshot, page, NOW)
    await db.commit()
    sender = SyntheticSender()
    assert await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    assert (await jobs(db))[0].event_type == "device_error"
    sensor.data_state = "FRESH"
    sensor.reason_codes = []
    snapshot.serverTime = NOW + timedelta(seconds=1)
    page = await active_snapshot_alerts(db, snapshot, include_health=True)
    await synchronize_snapshot(db, snapshot, page, page.computedAt)
    await db.commit()
    assert await EmailDeliveryWorker(sender).deliver_one(factory, page.computedAt)
    assert any(j.phase == "RECOVERY" and j.status == "SENT" for j in await jobs(db))


async def test_policy_delay_change_is_applied_before_claim(client, mail):
    db, policy, _, factory = mail
    await collect(client, db)
    policy.send_delay_minutes = 2
    await db.commit()
    sender = SyntheticSender()
    assert await EmailDeliveryWorker(sender).deliver_one(factory, NOW)
    job = (await jobs(db))[0]
    assert job.status == "QUEUED"
    assert job.due_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(minutes=2)
    assert sender.requests == []
