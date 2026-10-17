"""SQLite adapter for the framework-independent decision service."""

from __future__ import annotations

import logging
from typing import Any

from nonebot.log import LoguruHandler

from src.gatekeeper import decisions

from .async_storage import run_storage
from .request_record import JoinRequestAudit, JoinRequestRecordManager


def configure_decision_logging() -> None:
    """Forward core diagnostics to the Bot's existing console and daily file sinks."""
    decision_logger = logging.getLogger(decisions.__name__)
    if not any(isinstance(handler, LoguruHandler) for handler in decision_logger.handlers):
        decision_logger.addHandler(LoguruHandler())
    # Leave filtering to the configured NoneBot/Loguru sinks.
    decision_logger.setLevel(logging.DEBUG)
    decision_logger.propagate = False


class SqliteDecisionStorage:
    """Keep the record manager's transactions on the shared storage executor."""

    async def add_audit(self, **kwargs: Any) -> JoinRequestAudit | None:
        return await run_storage(JoinRequestRecordManager.add_audit, **kwargs)

    async def begin_pending_decision(self, **kwargs: Any) -> JoinRequestAudit | None:
        return await run_storage(JoinRequestRecordManager.begin_pending_decision, **kwargs)

    async def finalize_pending_decision(
        self, audit_id: int, *, applied: bool, failure_reason: str = ""
    ) -> bool:
        return await run_storage(
            JoinRequestRecordManager.finalize_pending_decision,
            audit_id,
            applied=applied,
            failure_reason=failure_reason,
        )

    async def get_pending_decision_count(self) -> int:
        return await run_storage(JoinRequestRecordManager.get_pending_decision_count)

    async def get_pending_decisions(self, limit: int = 20) -> list[JoinRequestAudit]:
        return await run_storage(JoinRequestRecordManager.get_pending_decisions, limit)
