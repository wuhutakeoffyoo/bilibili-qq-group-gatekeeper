"""Shared, bounded runtime for Bilibili API workflows."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from .bili_api import BiliApi, create_bili_http_client


T = TypeVar("T")


class BiliRequestCoordinator:
    """Reuse connections and cap concurrent, long-running Bilibili checks."""

    def __init__(self, *, max_concurrent: int = 4, total_timeout: float = 60.0):
        self.max_concurrent = max_concurrent
        self.total_timeout = total_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None
        self._verified_cookie_digest = ""
        self._verified_common_targets: set[int] = set()

    async def _ensure_runtime(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop and self._client is not None:
            return
        if self._client is not None:
            await self._client.aclose()
        self._loop = loop
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._client = create_bili_http_client(
            max_connections=self.max_concurrent * 2,
            max_keepalive_connections=self.max_concurrent,
        )

    async def run(
        self,
        cookie: str,
        operation: Callable[[BiliApi], Awaitable[T]],
    ) -> T:
        await self._ensure_runtime()
        assert self._semaphore is not None
        assert self._client is not None
        async def execute() -> T:
            assert self._semaphore is not None
            assert self._client is not None
            async with self._semaphore:
                api = BiliApi(cookie, client=self._client)
                return await operation(api)

        return await asyncio.wait_for(execute(), timeout=self.total_timeout)

    @staticmethod
    def _cookie_digest(cookie: str) -> str:
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    def set_verified_common_targets(self, cookie: str, target_uids: set[int]) -> None:
        """Remember targets known to be followed by the current login account."""
        self._verified_cookie_digest = self._cookie_digest(cookie)
        self._verified_common_targets = set(target_uids)

    def common_negative_is_definitive(
        self, cookie: str, target_uids: list[int]
    ) -> bool:
        return (
            self._verified_cookie_digest == self._cookie_digest(cookie)
            and set(target_uids).issubset(self._verified_common_targets)
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._semaphore = None
        self._loop = None
        self._verified_cookie_digest = ""
        self._verified_common_targets.clear()
