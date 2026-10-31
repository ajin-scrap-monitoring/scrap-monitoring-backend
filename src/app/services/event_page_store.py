"""Bounded process-local immutable event lists; tokens never bypass session authentication."""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException


@dataclass(frozen=True)
class PageContext:
    pit: str
    binding: str
    category: str
    page_size: int
    before: int
    after: int
    lower: datetime
    upper: datetime
    as_of: datetime


@dataclass(frozen=True)
class FrozenEventList:
    owner: str
    context: PageContext
    items: tuple[bytes, ...]
    token: str
    expires_at: datetime
    deadline: float
    size: int


class EventPageStore:
    """Synchronous mutations are atomic between awaits in one application event loop."""

    def __init__(
        self,
        *,
        ttl: int = 300,
        max_entries: int = 32,
        max_bytes: int = 32 * 1024 * 1024,
        max_owner_entries: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(ttl, max_entries, max_bytes, max_owner_entries) <= 0:
            raise ValueError("Snapshot cache limits must be positive")
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_owner_entries = max_owner_entries
        self.clock = clock
        self.entries: OrderedDict[str, FrozenEventList] = OrderedDict()
        self.size = 0
        self.builds = 0

    def begin(self) -> None:
        if self.builds >= 2:
            raise HTTPException(503, "EVENT_LIST_SNAPSHOT_BUSY")
        self.builds += 1

    def end(self) -> None:
        self.builds -= 1

    def _drop(self, token: str) -> None:
        self.size -= self.entries.pop(token).size

    def _prune(self) -> None:
        now = self.clock()
        for token, value in list(self.entries.items()):
            if value.deadline <= now:
                self._drop(token)

    def get(self, token: str, owner: str) -> FrozenEventList:
        self._prune()
        value = self.entries.get(token)
        if value is None or value.owner != owner:
            # Do not reveal whether a token belongs to another account.
            raise HTTPException(410, "EVENT_LIST_SNAPSHOT_GONE")
        return value

    def put(
        self, owner: str, context: PageContext, items: list[bytes], now: datetime
    ) -> FrozenEventList:
        self._prune()
        size = 1024 + sum(len(item) + 64 for item in items)
        if size > self.max_bytes:
            raise HTTPException(422, "EVENT_LIST_SNAPSHOT_TOO_LARGE")
        owned = [key for key, value in self.entries.items() if value.owner == owner]
        while len(owned) >= self.max_owner_entries:
            self._drop(owned.pop(0))
        while len(self.entries) >= self.max_entries or self.size + size > self.max_bytes:
            self._drop(next(iter(self.entries)))
        token = uuid4().hex
        while token in self.entries:
            token = uuid4().hex
        value = FrozenEventList(
            owner,
            context,
            tuple(items),
            token,
            now + timedelta(seconds=self.ttl),
            self.clock() + self.ttl,
            size,
        )
        self.entries[token] = value
        self.size += size
        return value
