"""Argon2id password work runs outside the event loop, one hash operation at a time."""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

# OWASP Argon2id minimum: 19 MiB, two iterations, one lane. Bound CPU/memory on edge hosts.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="password-hash")


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(32))


def _verify(encoded: str | None, password: str) -> bool:
    dummy = _dummy_hash()
    try:
        verified = _hasher.verify(encoded or dummy, password)
        return verified and encoded is not None
    except (VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_executor, _hasher.hash, password)


async def verify_password(encoded: str | None, password: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_executor, _verify, encoded, password)


def validate_new_password(password: str) -> None:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
