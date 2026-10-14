"""One-time initialization from private configuration, never an authentication fallback."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.passwords import hash_password, validate_new_password
from src.app.models.auth import AuthUser


async def bootstrap_accounts(db: AsyncSession) -> None:
    if await db.scalar(select(func.count()).select_from(AuthUser)):
        await db.rollback()
        return
    settings = get_settings()
    if not settings.ADMIN_PASSWORD:
        raise ValueError("Empty account database requires an explicit ADMIN_PASSWORD")
    credentials = [("admin", "관리자", "administrator", settings.ADMIN_PASSWORD.get_secret_value())]
    if settings.OPERATOR_PASSWORD:
        credentials.append(
            ("operator", "현장 운영자", "viewer", settings.OPERATOR_PASSWORD.get_secret_value())
        )
    for _, _, _, password in credentials:
        validate_new_password(password)
    # Release the initial read before password hashing, then atomically insert both accounts.
    await db.rollback()
    users = [
        AuthUser(
            username=name,
            display_name=display,
            role=role,
            password_hash=await hash_password(password),
        )
        for name, display, role, password in credentials
    ]
    db.add_all(users)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Concurrent startup may already have committed the same initial account set.
        if await db.get(AuthUser, "admin") is None:
            raise
        await db.rollback()
