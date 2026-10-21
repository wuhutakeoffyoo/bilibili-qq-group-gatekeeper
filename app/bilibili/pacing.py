"""Shared per-request pacing and bounded observation caching."""
from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import math
import os
import time
from collections import OrderedDict


class RequestLimiter:
    def __init__(self, rate=2.0, *, clock=time.monotonic, sleep=asyncio.sleep):
        selected = float(rate)
        if not math.isfinite(selected) or selected <= 0:
            raise ValueError('B站请求速率必须是大于0的有限数字')
        self.rate = min(10., max(0.1, selected))
        self.clock, self.sleep = clock, sleep
        self._loop = None
        self._lock = None
        self._next = 0.0
        self.waiting = 0
        self.requests = 0

    async def acquire(self):
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop, self._lock, self._next = loop, asyncio.Lock(), 0.0
        self.waiting += 1
        try:
            async with self._lock:
                delay = max(0.0, self._next - self.clock())
                if delay:
                    await self.sleep(delay)
                self._next = self.clock() + 1.0 / self.rate
                self.requests += 1
        finally:
            self.waiting -= 1

    def metrics(self):
        return {'requests': self.requests, 'waiting': self.waiting, 'requests_per_second': self.rate}


HTTP_LIMITER = RequestLimiter()


class ObservationCache:
    def __init__(self, ttl=60.0, max_entries=1024, *, clock=time.monotonic):
        self.ttl, self.max_entries, self.clock = ttl, max_entries, clock
        self.values = OrderedDict()
        self.inflight = {}
        self.waiters = {}
        self.hits = 0
        self.generation = 0

    def clear(self):
        self.values.clear()
        self.generation += 1

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()
        self.waiters.clear()
        self.clear()

    async def get_or_load(self, key, loader):
        now = self.clock()
        if key in self.values:
            expires, value = self.values.pop(key)
            if expires > now:
                self.values[key] = (expires, value)
                self.hits += 1
                return copy.deepcopy(value)
        current = self.inflight.get(key)
        if current is not None and current.get_loop() is asyncio.get_running_loop():
            return await self._await_shared(current)
        if len(self.inflight) >= self.max_entries:
            return await loader()

        generation = self.generation
        async def load():
            try:
                result = await loader()
                if generation == self.generation and getattr(result, 'state', '') in {'passed', 'failed'}:
                    self.values[key] = (self.clock() + self.ttl, copy.deepcopy(result))
                    while len(self.values) > self.max_entries:
                        self.values.popitem(last=False)
                return result
            finally:
                if self.inflight.get(key) is asyncio.current_task():
                    self.inflight.pop(key, None)

        task = asyncio.create_task(load())
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self.inflight[key] = task
        return await self._await_shared(task)

    async def _await_shared(self, task):
        self.waiters[task] = self.waiters.get(task, 0) + 1
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            remaining = self.waiters.get(task, 1) - 1
            if remaining:
                self.waiters[task] = remaining
            else:
                self.waiters.pop(task, None)
                if not task.done():
                    # Preserve shared work while anyone needs it, but do not
                    # keep consuming the HTTP queue after every deadline exits.
                    task.cancel()


OBSERVATIONS = ObservationCache()


def configure_pacing(settings):
    # Load after dotenv selection, not at module import.
    selected = RequestLimiter(getattr(settings, 'bili_requests_per_second', os.getenv('BILI_REQUESTS_PER_SECOND', '2')))
    HTTP_LIMITER.rate = selected.rate
    OBSERVATIONS.ttl = min(300., max(0., float(getattr(settings, 'bili_observation_ttl', 60))))


def cached_observation(function):
    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        cookie = hashlib.sha256(self.cookie.encode()).hexdigest()
        key = (function.__name__, cookie, self.cache_scope,
               json.dumps([args, kwargs], sort_keys=True, default=str, ensure_ascii=False))
        return await OBSERVATIONS.get_or_load(key, lambda: function(self, *args, **kwargs))
    return wrapped
