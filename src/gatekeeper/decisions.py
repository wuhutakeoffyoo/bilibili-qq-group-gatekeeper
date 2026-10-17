"""Durable external decisions, independent of the bot framework and persistence."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, Sequence

from .decision_runtime import DecisionLocks, identity_decision_keys

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionRequest:
    bot_id: str
    group_id: str
    sub_type: str
    flag: str

    @property
    def request_key(self) -> str:
        flag = self.flag.strip()
        return ":".join((self.bot_id, self.group_id, self.sub_type, flag)) if flag else ""


class DecisionAudit(Protocol):
    """Persisted fields needed to execute or manually resolve a decision."""

    @property
    def id(self) -> int | None: ...

    @property
    def qq(self) -> str: ...

    @property
    def bili_uid(self) -> int | None: ...

    @property
    def was_created(self) -> bool: ...

    @property
    def action_status(self) -> str: ...


class DecisionStorage(Protocol):
    """Async persistence boundary; each finalization remains one storage transaction."""

    async def add_audit(self, **kwargs: Any) -> DecisionAudit | None: ...

    async def begin_pending_decision(self, **kwargs: Any) -> DecisionAudit | None: ...

    async def finalize_pending_decision(
        self, audit_id: int, *, applied: bool, failure_reason: str = ""
    ) -> bool: ...

    async def get_pending_decision_count(self) -> int: ...

    async def get_pending_decisions(self, limit: int = 20) -> Sequence[DecisionAudit]: ...


class DefiniteActionFailure(Exception):
    """The adapter received a definite failure rather than an uncertain outcome."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


@dataclass(frozen=True)
class ResolutionResult:
    found: bool
    finalized: bool = False
    remaining: int = 0


class DecisionService:
    """Persist intent before an external action and pause on uncertain results.

    The caller shares ``locks`` with its identity reads and binding changes.
    Request serialization prevents simultaneous copies from executing twice.
    An action with an unknown result is left pending for manual confirmation;
    no retry is performed by this service.
    """

    def __init__(self, storage: DecisionStorage, *, locks: DecisionLocks | None = None) -> None:
        self.storage = storage
        self.locks = locks if locks is not None else DecisionLocks()
        self.paused = False

    async def execute(
        self,
        request: DecisionRequest,
        *,
        audit_kwargs: dict[str, Any],
        perform_action: Callable[[], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]],
    ) -> bool:
        request_key = request.request_key
        async with self.locks.hold(f"request:{request_key}" if request_key else ""):
            return await self._execute(
                request_key,
                audit_kwargs=audit_kwargs,
                perform_action=perform_action,
                notify=notify,
            )

    async def _execute(
        self,
        request_key: str,
        *,
        audit_kwargs: dict[str, Any],
        perform_action: Callable[[], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]],
    ) -> bool:
        if self.paused:
            paused_kwargs = dict(audit_kwargs)
            paused_kwargs["result"] = "ignored"
            paused_kwargs["reasons"] = ["存在尚未人工确认的历史审批，自动审批已暂停"]
            paused_audit = await self.storage.add_audit(**paused_kwargs)
            if paused_audit is None:
                logger.critical("自动审批暂停期间的忽略审计写入失败")
            await notify("自动审批已暂停：存在尚未确认的历史审批。请使用 /待确认审批 查看。")
            return False

        pending_audit = await self.storage.begin_pending_decision(**audit_kwargs, request_key=request_key)
        if pending_audit is None or pending_audit.id is None:
            self.paused = True
            logger.critical("审批前置审计写入失败，已停止执行 QQ 审批动作")
            await notify(
                f"严重：QQ {audit_kwargs.get('qq', '未知')} 的审批前置审计写入失败，已安全地停止自动审批。"
            )
            return False

        if not pending_audit.was_created:
            if pending_audit.action_status == "applied":
                logger.info("忽略已完成的重复加群请求：request_key=%s", request_key)
                return True
            if pending_audit.action_status == "pending":
                self.paused = True
                await notify(f"检测到重复的未确认审批 #{pending_audit.id}，已暂停自动审批，请人工核对。")
            else:
                logger.warning(
                    "忽略已终止的重复加群请求：request_key=%s, status=%s",
                    request_key,
                    pending_audit.action_status,
                )
            return False

        try:
            await perform_action()
        except DefiniteActionFailure as exc:
            logger.exception("调用加群审批接口失败")
            failure_reason = f"调用加群审批接口失败，申请保持未处理状态：{exc.error_type}"
            finalized = await self.storage.finalize_pending_decision(
                pending_audit.id, applied=False, failure_reason=failure_reason
            )
            if not finalized:
                self.paused = True
                await notify(f"严重：审批接口失败后无法更新审计 #{pending_audit.id}，请检查 SQLite。")
            return False
        except asyncio.CancelledError:
            self.paused = True
            logger.warning("审批调用被取消，审计 #%s 保留待确认状态", pending_audit.id)
            raise
        except Exception as exc:
            self.paused = True
            logger.exception("审批结果无法确认，保留 pending 审计并暂停自动审批")
            await notify(
                f"审批 #{pending_audit.id} 结果无法确认（{type(exc).__name__}）。"
                "自动审批已暂停，请核对 QQ 群的实际结果，再使用 /确认审批 处理。"
            )
            return False

        finalized = await self.storage.finalize_pending_decision(pending_audit.id, applied=True)
        if not finalized:
            self.paused = True
            logger.critical("QQ 审批已执行，但审计状态确认失败：id=%s", pending_audit.id)
            await notify(
                f"严重：QQ 审批已执行，但审计 #{pending_audit.id} 仍未确认。请勿重复审批，并检查 SQLite。"
            )
            return False
        return True

    async def restore_pending(self) -> int:
        """Restore the global pause after process startup without retrying actions."""
        pending_count = await self.storage.get_pending_decision_count()
        self.paused = pending_count > 0
        if self.paused:
            logger.critical("检测到 %s 条尚未确认的审批审计，请在执行新审批前人工核对", pending_count)
        return pending_count

    async def resolve_pending(self, audit_id: int, *, applied: bool) -> ResolutionResult:
        """Record an administrator's confirmed result and recount pending actions."""
        pending = await self.storage.get_pending_decisions(1000)
        audit = next((audit for audit in pending if audit.id == audit_id), None)
        if audit is None:
            return ResolutionResult(found=False)
        async with self.locks.hold(*identity_decision_keys(audit.qq, audit.bili_uid)):
            finalized = await self.storage.finalize_pending_decision(
                audit_id, applied=applied, failure_reason="超级管理员确认 QQ 审批未生效"
            )
        if not finalized:
            return ResolutionResult(found=True)
        remaining = await self.storage.get_pending_decision_count()
        self.paused = remaining > 0
        return ResolutionResult(found=True, finalized=True, remaining=remaining)
