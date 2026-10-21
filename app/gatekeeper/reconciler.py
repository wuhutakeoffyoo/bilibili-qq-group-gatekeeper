"""Cancellable reconciliation loop using an injected request-specific inspector."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .decisions import RequestEvidence, ScopedDecisionService

logger = logging.getLogger(__name__)


async def _silent_notify(message: str) -> None:
    logger.warning("%s", message)


class DecisionReconciler:
    def __init__(
        self, service: ScopedDecisionService, *,
        inspector: Callable[[dict[str, Any]], Awaitable[RequestEvidence]],
        replay: Callable[[dict[str, Any]], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]] = _silent_notify,
        interval: float = 30.0,
    ) -> None:
        self.service, self.inspector, self.replay, self.notify = service, inspector, replay, notify
        self.interval = max(0.01, interval)
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> dict[int, str]:
        results = {}
        for audit in await self.service.pending_snapshot():
            if audit.id is None:
                continue
            try:
                results[audit.id] = await self.service.reconcile_pending(
                    audit.id, inspector=self.inspector, replay=self.replay, notify=self.notify,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reconciliation failed for audit %s", audit.id)
                results[audit.id] = "error"
        return results

    async def _run(self) -> None:
        while True:
            delay = self.interval
            try:
                await self.run_once()
                for audit in await self.service.pending_snapshot():
                    if audit.id is None:
                        continue
                    state = await self.service.state_store.load(audit.id)
                    if not state or state.get("phase") == "manual":
                        continue
                    for field in ("next_inspection_at", "next_retry_at"):
                        remaining = float(state.get(field, 0)) - self.service.clock()
                        if remaining > 0:
                            delay = min(delay, max(0.01, remaining))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pending decision reconciliation failed; durable intents remain pending")
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="gatekeeper-reconciliation")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
