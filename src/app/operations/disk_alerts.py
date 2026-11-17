"""Host capacity alerts, independent of the web app, business DB and recipient API."""

import configparser
import hashlib
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.app.services.gmail_sender import GmailError

Level = Literal["NORMAL", "WARNING", "CRITICAL", "UNKNOWN"]
MAX_BYTES = 128 * 1024


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class DiskTarget(PrivateModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,50}$")
    path: str = Field(min_length=1, max_length=4096, repr=False)
    require_mount: bool = False

    @model_validator(mode="after")
    def absolute(self) -> "DiskTarget":
        if not Path(self.path).is_absolute():
            raise ValueError("An absolute filesystem path is required")
        return self


class DiskAlertConfig(PrivateModel):
    enabled: bool = False
    server_name: str = Field(default="backend-server", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    recipients: list[str] = Field(default_factory=list, max_length=20, repr=False)
    targets: list[DiskTarget] = Field(min_length=1, max_length=16)
    warning_percent: float = Field(default=75, ge=1, le=99)
    critical_percent: float = Field(default=90, ge=1, le=100)
    critical_recovery_percent: float | None = Field(default=None, ge=0, le=99)
    recovery_percent: float | None = Field(default=None, ge=0, le=98)
    interval_seconds: int = Field(default=300, ge=30, le=86400)
    reminder_seconds: int = Field(default=3600, ge=300, le=604800)

    @property
    def normal_threshold(self) -> float:
        return (
            self.recovery_percent
            if self.recovery_percent is not None
            else max(0.0, self.warning_percent - 5)
        )

    @property
    def critical_exit_threshold(self) -> float:
        return (
            self.critical_recovery_percent
            if self.critical_recovery_percent is not None
            else self.critical_percent
            - min(5.0, (self.critical_percent - self.warning_percent) / 2)
        )

    @model_validator(mode="after")
    def valid(self) -> "DiskAlertConfig":
        import re

        if (
            not self.normal_threshold
            < self.warning_percent
            < self.critical_exit_threshold
            < self.critical_percent
        ):
            raise ValueError("Require recovery < warning < critical")
        if len({t.name for t in self.targets}) != len(self.targets):
            raise ValueError("Duplicate disk label")
        if len({x.casefold() for x in self.recipients}) != len(self.recipients):
            raise ValueError("Duplicate recipient")
        if any(
            not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x) or len(x) > 255
            for x in self.recipients
        ):
            raise ValueError("Invalid recipient")
        if self.enabled and not self.recipients:
            raise ValueError("Enabled delivery requires recipients")
        return self


class Delivery(PrivateModel):
    status: Literal["PENDING", "ACCEPTED", "UNKNOWN", "REJECTED"]
    attempted_at: float = Field(ge=0, allow_inf_nan=False)
    identifier: str = Field(pattern=r"^[a-f0-9]{32}$")


class DiskState(PrivateModel):
    level: Level
    deliveries: dict[str, Delivery] = Field(default_factory=dict, max_length=20)


class MonitorState(PrivateModel):
    version: Literal[1]
    disks: dict[str, DiskState] = Field(max_length=16)


class Sample(PrivateModel):
    name: str
    level: Level
    used_percent: float | None
    available_bytes: int | None


class Sender(Protocol):
    async def send(self, recipient: str, subject: str, body: str, identifier: str) -> str: ...


def read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Private regular file required")
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Private file too large")
    return data


def load_config(path: Path) -> DiskAlertConfig:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(read_private(path).decode("utf-8"))
    if parser.defaults() or "monitor" not in parser:
        raise ValueError("Explicit monitor section required")
    data: dict[str, object] = dict(parser["monitor"])
    if "recipients" in data:
        data["recipients"] = [
            x.strip() for x in str(data["recipients"]).replace(",", "\n").splitlines() if x.strip()
        ]

    def boolean(value: str) -> bool:
        if value.lower() not in ("true", "false"):
            raise ValueError("Use true or false")
        return value.lower() == "true"

    if "enabled" in data:
        data["enabled"] = boolean(str(data["enabled"]))
    for key in (
        "warning_percent",
        "critical_percent",
        "critical_recovery_percent",
        "recovery_percent",
    ):
        if key in data:
            data[key] = float(str(data[key]))
    for key in ("interval_seconds", "reminder_seconds"):
        if key in data:
            data[key] = int(str(data[key]))
    targets = []
    for name in parser.sections():
        if name == "monitor":
            continue
        if not name.startswith("disk:"):
            raise ValueError("Unexpected configuration section")
        target: dict[str, object] = dict(parser[name])
        if "name" in target:
            raise ValueError("Disk name belongs in section header")
        target["name"] = name.removeprefix("disk:")
        if "require_mount" in target:
            target["require_mount"] = boolean(str(target["require_mount"]))
        targets.append(target)
    if "targets" in data:
        raise ValueError("Use disk sections")
    data["targets"] = targets
    return DiskAlertConfig.model_validate(data)


def write_state(path: Path, state: BaseModel) -> None:
    # The dedicated state directory must already exist; never create arbitrary paths.
    if path.is_symlink() or path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Private state directory required")
    data = state.model_dump_json().encode()
    if len(data) > MAX_BYTES:
        raise ValueError("State too large")
    descriptor, temporary = tempfile.mkstemp(prefix=".disk-alert-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sample(target: DiskTarget, config: DiskAlertConfig, previous: Level) -> Sample:
    try:
        if target.require_mount and not os.path.ismount(target.path):
            raise ValueError("Expected filesystem is not mounted")
        info = os.statvfs(target.path)
        used = info.f_blocks - info.f_bfree
        capacity = used + info.f_bavail
        if capacity <= 0 or used < 0 or info.f_bavail < 0:
            raise ValueError("Invalid filesystem statistics")
        percent = 100 * used / capacity
        level: Level = "NORMAL"
        if percent >= config.critical_percent or (
            previous == "CRITICAL" and percent > config.critical_exit_threshold
        ):
            level = "CRITICAL"
        elif percent >= config.warning_percent or (
            previous in ("WARNING", "CRITICAL") and percent > config.normal_threshold
        ):
            level = "WARNING"
        return Sample(
            name=target.name,
            level=level,
            used_percent=percent,
            available_bytes=info.f_bavail * info.f_frsize,
        )
    except (OSError, ValueError):
        return Sample(name=target.name, level="UNKNOWN", used_percent=None, available_bytes=None)


async def check(
    config: DiskAlertConfig, path: Path, sender: Sender, now: float, *, send: bool = False
) -> list[Sample]:
    if not math.isfinite(now) or now < 0:
        raise ValueError("Invalid clock")
    state = (
        MonitorState.model_validate_json(read_private(path))
        if path.exists() or path.is_symlink()
        else MonitorState(version=1, disks={})
    )
    state.disks = {k: v for k, v in state.disks.items() if k in {t.name for t in config.targets}}
    results = []
    active = send and config.enabled
    for target in config.targets:
        old = state.disks.get(target.name)
        previous_level = old.level if old else None
        current = sample(target, config, old.level if old else "NORMAL")
        results.append(current)
        if not active:
            continue  # Inspection never consumes a notification or changes delivery state.
        changed = old is None or old.level != current.level
        if changed:
            old = DiskState(level=current.level)
            state.disks[target.name] = old
        assert old is not None
        addresses = {
            hashlib.sha256(r.casefold().encode()).hexdigest(): r for r in config.recipients
        }
        old.deliveries = {k: v for k, v in old.deliveries.items() if k in addresses}
        if current.level == "NORMAL" and previous_level in (None, "NORMAL"):
            continue
        for key, recipient in addresses.items():
            delivery = old.deliveries.get(key)
            if delivery:
                if delivery.status in ("PENDING", "UNKNOWN"):
                    continue  # Includes a process interrupted after a possible provider acceptance.
                if (
                    current.level == "NORMAL"
                    or now - delivery.attempted_at < config.reminder_seconds
                ):
                    continue
            identifier = uuid4().hex
            old.deliveries[key] = Delivery(
                status="PENDING", attempted_at=now, identifier=identifier
            )
            write_state(path, state)  # Durable intent precedes the external side effect.
            try:
                await sender.send(
                    recipient,
                    f"[Server capacity] {config.server_name}/{target.name}: {current.level}",
                    f"Filesystem: {target.name}\nState: {current.level}\n"
                    f"Used percent: {current.used_percent}\n"
                    f"Available bytes: {current.available_bytes}\n"
                    "Inspect storage and retention policy; no automatic deletion was performed.",
                    identifier,
                )
            except GmailError as error:
                old.deliveries[key].status = "UNKNOWN" if error.uncertain else "REJECTED"
            else:
                old.deliveries[key].status = "ACCEPTED"
            write_state(path, state)
    if active:
        write_state(path, state)
    return results
