"""Async boundary for the synchronous SQLite record managers."""

from __future__ import annotations

import asyncio
import concurrent.futures
from functools import partial
from typing import Any, Callable, TypeVar


T = TypeVar("T")
_STORAGE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="gatekeeper-sqlite"
)


async def run_storage(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking storage operation outside NoneBot's event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STORAGE_EXECUTOR, partial(function, *args, **kwargs)
    )
