"""Synthetic complete-database fixture shared by transfer tests and Docker verification."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, insert, null
from sqlalchemy.ext.asyncio import create_async_engine

from src.app import models  # noqa: F401 -- register all tables
from src.app.core.passwords import hash_password
from src.app.db.base import Base
from src.app.db.migration import upgrade_schema
from src.app.services.session_service import token_digest

COOKIE = "synthetic-transfer-session-token-" + "x" * 32
PASSWORD = "synthetic-transfer-login"


async def seed_transfer_source(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    now = datetime.now(UTC).replace(microsecond=123456)
    password_hash = await hash_password(PASSWORD)
    try:
        async with engine.begin() as db:
            await db.run_sync(upgrade_schema)
            for table in Base.metadata.sorted_tables:
                row = {}
                for column in table.columns:
                    if isinstance(column.type, JSON):
                        row[column.name] = {"synthetic": [1, None, "한글", 0.725, 2**54 + 9]}
                    elif isinstance(column.type, Boolean):
                        row[column.name] = True
                    elif isinstance(column.type, DateTime):
                        row[column.name] = now
                    elif isinstance(column.type, Float):
                        row[column.name] = 72.5
                    elif isinstance(column.type, Integer):
                        row[column.name] = (
                            42 if column.name in ("id", "alert_id", "last_alert_id") else 1
                        )
                    elif isinstance(column.type, String):
                        row[column.name] = ("synthetic-" + column.name)[: column.type.length or 100]
                    else:
                        raise AssertionError(column.type)
                if "username" in row:
                    row["username"] = "transfer-user"
                if "pit_id" in row:
                    row["pit_id"] = "pit-synthetic"
                if table.name == "pit_configs":
                    row.update(
                        id="pit-synthetic",
                        total_depth_cm=300,
                        warning_threshold_percent=75,
                        critical_threshold_percent=90,
                    )
                if table.name == "alert_logs":
                    row.update(
                        event_type="collection_required",
                        severity="error",
                        channel="LOG",
                        status="RECORDED",
                    )
                if table.name == "auth_users":
                    row.update(role="viewer", password_hash=password_hash)
                if table.name == "auth_sessions":
                    row.update(
                        token_hash=token_digest(COOKIE),
                        csrf_token="synthetic-csrf",
                        expires_at=now + timedelta(days=1),
                    )
                if table.name == "alert_policies":
                    row.update(event_types=["collection_required", "device_error"], version_id=7)
                if table.name == "edge_v2_messages":
                    row.update(sequence=2**54 + 9, message_type="edge_status", body_hash="a" * 64)
                if table.name == "edge_v2_artifacts":
                    row.update(artifact_key=["scan", 123], key_hash="b" * 64)
                if table.name == "notification_recipients":
                    row.update(email="synthetic@example.invalid", channels="email")
                if table.name == "media_clip_jobs":
                    row.update(snapshot=None, next_attempt_at=None, clip_id=None)
                await db.execute(insert(table), row)
            users = Base.metadata.tables["auth_users"]
            original_user = dict((await db.execute(users.select())).mappings().one())
            for name in ("Z-user", "a-user", "한글-user"):
                await db.execute(insert(users), {**original_user, "username": name})
            # Cover both JSON null and SQL NULL in the one nullable JSON column.
            job = Base.metadata.tables["media_clip_jobs"]
            second = dict((await db.execute(job.select())).mappings().one())
            second.update(id=43, event_id="synthetic-other-event", snapshot=None)
            await db.execute(
                insert(job).values(snapshot=null()),
                {key: value for key, value in second.items() if key != "snapshot"},
            )
    finally:
        await engine.dispose()
    return password_hash
