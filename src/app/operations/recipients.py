"""Read-only recipient projection with an explicitly bounded private fallback cache."""

import asyncio
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.app.models.recipient import NotificationRecipientModel

MAX_RECIPIENTS = 1000
MAX_CACHE_BYTES = 512 * 1024


class OperationsRecipient(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=50)
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", repr=False)
    version: int = Field(ge=1, strict=True)


class RecipientSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    format_version: int = Field(default=1, ge=1, le=1, strict=True)
    generated_at: AwareDatetime
    recipients: list[OperationsRecipient] = Field(max_length=MAX_RECIPIENTS, repr=False)

    @model_validator(mode="after")
    def unique(self) -> "RecipientSnapshot":
        if len({r.id for r in self.recipients}) != len(self.recipients) or len(
            {r.email.casefold() for r in self.recipients}
        ) != len(self.recipients):
            raise ValueError("Duplicate recipient projection")
        return self


class RecipientCacheError(RuntimeError):
    """Safe error code only; no address, credentials, SQL or path diagnostics."""


async def read_recipients(
    factory: async_sessionmaker[AsyncSession], now: datetime
) -> RecipientSnapshot:
    async with factory() as db:
        if (await db.connection()).dialect.name == "postgresql":
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = (
            await db.execute(
                select(
                    NotificationRecipientModel.id,
                    NotificationRecipientModel.email,
                    NotificationRecipientModel.version_id,
                    NotificationRecipientModel.channels,
                )
                .where(
                    NotificationRecipientModel.enabled.is_(True),
                    NotificationRecipientModel.operations_email_enabled.is_(True),
                )
                .order_by(NotificationRecipientModel.id)
                .limit(MAX_RECIPIENTS + 1)
            )
        ).all()
        if len(rows) > MAX_RECIPIENTS:
            raise RecipientCacheError("OPERATIONS_RECIPIENT_LIMIT")
        return RecipientSnapshot(
            generated_at=now.astimezone(UTC),
            recipients=[
                OperationsRecipient(id=r.id, email=r.email, version=r.version_id)
                for r in rows
                if "email" in r.channels.split(",")
            ],
        )


def write_snapshot(path: Path, snapshot: RecipientSnapshot) -> None:
    payload = snapshot.model_dump_json().encode()
    if len(payload) > MAX_CACHE_BYTES:
        raise RecipientCacheError("OPERATIONS_RECIPIENT_LIMIT")
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Symlink cache")
        descriptor, temporary = tempfile.mkstemp(prefix=".operations-recipients-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise RecipientCacheError("OPERATIONS_RECIPIENT_CACHE_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_snapshot(path: Path, now: datetime, max_age_seconds: int) -> RecipientSnapshot:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Cache must be a private regular file")
            payload = file.read(MAX_CACHE_BYTES + 1)
        if len(payload) > MAX_CACHE_BYTES:
            raise ValueError("Cache too large")
        snapshot = RecipientSnapshot.model_validate_json(payload)
        age = (now - snapshot.generated_at).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise ValueError("Cache expired or from future")
        return snapshot
    except (OSError, ValueError):
        raise RecipientCacheError("OPERATIONS_RECIPIENT_CACHE_UNAVAILABLE") from None


async def refresh_recipients(
    factory: async_sessionmaker[AsyncSession],
    path: Path,
    now: datetime,
    *,
    max_age_seconds: int = 86400,
    timeout_seconds: float = 8,
) -> tuple[RecipientSnapshot, bool]:
    """Return (snapshot, using_cache). A successful empty selection revokes old targets."""
    if now.tzinfo is None or not 1 <= max_age_seconds <= 86400 or not 0 < timeout_seconds <= 30:
        raise RecipientCacheError("OPERATIONS_RECIPIENT_CONFIG_INVALID")
    try:
        async with asyncio.timeout(timeout_seconds):
            snapshot = await read_recipients(factory, now)
    except Exception:
        # Cancellation is a BaseException and must propagate. Do not overwrite the
        # last successful projection or treat an unavailable DB as an empty selection.
        return read_snapshot(path, now, max_age_seconds), True
    write_snapshot(path, snapshot)
    return snapshot, False
