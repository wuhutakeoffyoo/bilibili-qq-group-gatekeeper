"""Process-local serialization for requests and identity binding decisions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class DecisionLocks:
    """Hold sorted keys through a decision; discard locks after their last waiter."""

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def hold(self, *keys: str) -> AsyncIterator[None]:
        entries = []
        acquired = []
        for key in sorted(set(keys) - {""}):
            entry = self._entries.setdefault(key, _LockEntry())
            entry.users += 1
            entries.append((key, entry))
        try:
            for _, entry in entries:
                await entry.lock.acquire()
                acquired.append(entry)
            yield
        finally:
            for entry in reversed(acquired):
                entry.lock.release()
            for key, entry in entries:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key)


def identity_decision_keys(qq: str, bili_uid: int | None) -> tuple[str, ...]:
    keys = [f"identity:qq:{qq}"]
    if bili_uid is not None:
        keys.append(f"identity:uid:{bili_uid}")
    return tuple(keys)
