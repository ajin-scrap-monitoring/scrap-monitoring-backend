"""Observe cached media cleanup state; never execute cleanup or query the business DB."""

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.app.operations.disk_alerts import (
    Delivery,
    DiskAlertConfig,
    PrivateModel,
    Sender,
    read_private,
    write_state,
)
from src.app.services.gmail_sender import GmailError
from src.app.services.media_client import MediaClient, MediaServiceError


class Upstream(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)


class Pressure(Upstream):
    used_bytes: int = Field(ge=0)
    limit_bytes: int = Field(gt=0)
    percent: float = Field(ge=0)
    shortfall_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> "Pressure":
        if abs(
            self.percent - self.used_bytes * 100 / self.limit_bytes
        ) > 0.00001 or self.shortfall_bytes != max(0, self.used_bytes - self.limit_bytes):
            raise ValueError("Inconsistent pressure")
        return self


class Measurement(Upstream):
    pressures: dict[str, Pressure] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def limits(self) -> "Measurement":
        keys = set(self.pressures)
        if not {"daily_budget", "minimum_free"} <= keys or not keys <= {
            "daily_budget",
            "total_budget",
            "minimum_free",
        }:
            raise ValueError("Unknown pressure scope")
        return self


class CleanupPolicy(Upstream):
    enabled: bool
    stop_percent: int = Field(ge=1, le=99)
    start_percent: int = Field(ge=2, le=99)
    critical_percent: int = Field(ge=3, le=100)
    interval_seconds: int = Field(ge=5, le=3600)
    notification_seconds: int = Field(ge=60, le=86400)

    @model_validator(mode="after")
    def ordered(self) -> "CleanupPolicy":
        if not self.stop_percent < self.start_percent < self.critical_percent:
            raise ValueError("Unordered cleanup thresholds")
        return self


class LastAttempt(Upstream):
    success: bool
    space_recovery_confirmed: bool


class CleanupStatus(Upstream):
    enabled: bool
    configuration_error: str | None = Field(max_length=100, pattern=r"^[A-Z0-9_]+$")
    active: bool
    mode: Literal["idle", "active", "blocked", "cooldown", "suspended", "error", "disabled"]
    observed_at: AwareDatetime | None
    valid_until_at: AwareDatetime | None
    state_fresh: bool
    measurement: Measurement | None
    policy: CleanupPolicy
    critical: bool
    alert_latched: bool
    notification_sequence: int = Field(ge=0)
    reason_code: str | None = Field(max_length=100, pattern=r"^[A-Z0-9_]+$")
    stalled: bool
    attempts: int = Field(ge=0)
    successful_days: int = Field(ge=0)
    observed_reclaimed_bytes: int = Field(ge=0)
    last_attempt: LastAttempt | None
    scope: Literal["closed_ready_daily_mp4"]
    minimum_retention_guaranteed: Literal[False]

    def usable(self, now: datetime) -> bool:
        observed, until = self.observed_at, self.valid_until_at
        if (
            not self.enabled
            or not self.policy.enabled
            or self.configuration_error is not None
            or self.mode == "disabled"
            or not self.state_fresh
            or observed is None
            or until is None
            or observed.utcoffset() != UTC.utcoffset(observed)
            or until.utcoffset() != UTC.utcoffset(until)
            or not observed <= now <= until
            or (until - observed).total_seconds() > 2 * self.policy.interval_seconds + 10.001
            or self.measurement is None
        ):
            return False
        critical = any(
            p.used_bytes * 100 >= p.limit_bytes * self.policy.critical_percent
            for p in self.measurement.pressures.values()
        )
        return self.critical == critical


class Channel(PrivateModel):
    active: bool
    signature: str
    deliveries: dict[str, Delivery] = Field(default_factory=dict, max_length=20)


class CleanupMailState(PrivateModel):
    version: Literal[1] = 1
    channels: dict[str, Channel] = Field(default_factory=dict, max_length=3)


class Notice(PrivateModel):
    channel: Literal["status", "capacity", "cleanup"]
    active: bool
    reason: str
    repeat_seconds: int
    detail: str


async def fetch_status(client: MediaClient) -> CleanupStatus:
    response = await client._request(
        "GET", "capacity-cleanup/status", (200,), daily=True, max_bytes=64 * 1024
    )
    if response.headers.get("cache-control") != "no-store":
        raise ValueError("Uncached status required")
    return CleanupStatus.model_validate_json(response.content)


def notices(
    status: CleanupStatus | None, state: CleanupMailState, now: float, reminder: int
) -> list[Notice]:
    if status is None or not status.usable(datetime.fromtimestamp(now, UTC)):
        # Do not clear either operational alarm using a stale, disabled or failed query.
        return [
            Notice(
                channel="status",
                active=True,
                reason="STATUS_UNAVAILABLE",
                repeat_seconds=reminder,
                detail="미디어 정리 상태를 최신의 유효한 관측으로 확인할 수 없습니다.",
            )
        ]
    assert status.measurement is not None
    below = all(
        p.used_bytes * 100 <= p.limit_bytes * status.policy.stop_percent
        for p in status.measurement.pressures.values()
    )
    old = state.channels.get("capacity")
    capacity = status.critical or status.alert_latched or bool(old and old.active and not below)
    blocked = (
        status.stalled
        or status.mode in ("blocked", "suspended", "error")
        or status.reason_code
        in {
            "DELETE_ERROR",
            "SPACE_NOT_RECLAIMED",
            "GENERATION_OUTPACES_CLEANUP",
            "FAILURE_LIMIT_REACHED",
            "MEASUREMENT_UNAVAILABLE",
        }
    )
    pressures = ", ".join(
        f"{k}={v.percent:.6f}%" for k, v in sorted(status.measurement.pressures.items())
    )
    reclaimed = status.last_attempt.space_recovery_confirmed if status.last_attempt else "시도 없음"
    detail = (
        f"관측 UTC: {status.observed_at.isoformat() if status.observed_at else '-'}\n"
        f"압박률: {pressures}\n정리 상태: {status.mode}\n사유: {status.reason_code or '-'}\n"
        f"정체: {status.stalled}\n시도: {status.attempts}\n"
        f"파일 정리 완료 건수: {status.successful_days}\n"
        f"관측된 공간 회수 순증가 합(byte): {status.observed_reclaimed_bytes}\n"
        f"최근 시도 공간 회수 확인: {reclaimed}\n"
        f"미디어 알림 순번: {status.notification_sequence}\n"
        "파일 정리 완료와 실제 공간 회수는 별개입니다. 이 점검은 삭제를 실행하지 않습니다."
    )
    return [
        Notice(
            channel="status",
            active=False,
            reason="STATUS_AVAILABLE",
            repeat_seconds=reminder,
            detail=(
                "미디어 정리 상태의 최신 조회가 다시 확인되었습니다. 공간 회복을 뜻하지 않습니다."
            ),
        ),
        Notice(
            channel="capacity",
            active=capacity,
            reason="CRITICAL" if capacity else "RECOVERED",
            repeat_seconds=status.policy.notification_seconds,
            detail=detail,
        ),
        Notice(
            channel="cleanup",
            active=blocked,
            reason=f"{status.mode}:{status.reason_code}:{status.stalled}" if blocked else "CLEAR",
            repeat_seconds=status.policy.notification_seconds,
            detail=detail,
        ),
    ]


async def check_cleanup(
    config: DiskAlertConfig,
    path: Path,
    sender: Sender,
    now: float,
    status: CleanupStatus | None,
    *,
    send: bool = False,
) -> list[Notice]:
    if not math.isfinite(now) or now < 0:
        raise ValueError("Invalid clock")
    state = (
        CleanupMailState.model_validate_json(read_private(path))
        if path.exists() or path.is_symlink()
        else CleanupMailState()
    )
    result = notices(status, state, now, config.reminder_seconds)
    if not send or not config.enabled:
        return result
    addresses = {hashlib.sha256(r.casefold().encode()).hexdigest(): r for r in config.recipients}
    for notice in result:
        old = state.channels.get(notice.channel)
        signature = f"{notice.active}:{notice.reason}"
        previous_active = bool(old and old.active)
        previous_recipients = set(old.deliveries) if old else set()
        if old is None or old.signature != signature:
            old = Channel(active=notice.active, signature=signature)
            state.channels[notice.channel] = old
        old.deliveries = {k: v for k, v in old.deliveries.items() if k in addresses}
        if not notice.active and not previous_active and not old.deliveries:
            continue  # No recovery message at first normal startup or for a new recipient.
        for key, recipient in addresses.items():
            delivery = old.deliveries.get(key)
            if (
                not notice.active
                and delivery is None
                and (not previous_active or key not in previous_recipients)
            ):
                continue  # A newly configured recipient never receives an unobserved recovery.
            if delivery is not None and (
                delivery.status in ("PENDING", "UNKNOWN")
                or delivery.status == "ACCEPTED"
                and not notice.active
                or now - delivery.attempted_at < notice.repeat_seconds
            ):
                continue
            identifier = uuid4().hex
            old.deliveries[key] = Delivery(
                status="PENDING", attempted_at=now, identifier=identifier
            )
            write_state(path, state)
            try:
                label = "ALERT" if notice.active else "RECOVERED"
                await sender.send(
                    recipient,
                    f"[Media cleanup] {config.server_name}/{notice.channel}: {label}",
                    notice.detail,
                    identifier,
                )
            except GmailError as exc:
                old.deliveries[key].status = "UNKNOWN" if exc.uncertain else "REJECTED"
            else:
                old.deliveries[key].status = "ACCEPTED"
            write_state(path, state)
    write_state(path, state)
    return result


async def observe(client: MediaClient) -> CleanupStatus | None:
    try:
        return await fetch_status(client)
    except (MediaServiceError, ValueError):
        return None
