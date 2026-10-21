"""Durable approvals which reserve only the affected request and identities."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.database.async_storage import run_storage
from app.database.legacy import database_connection
from src.gatekeeper.decision_runtime import DecisionLocks, identity_decision_keys
from src.gatekeeper.decisions import (
    DecisionAudit,
    DecisionRequest,
    DecisionService,
    DecisionStorage,
    DefiniteActionFailure,
    ResolutionResult,
)
from src.gatekeeper.formatters import build_reject_reason

logger = logging.getLogger(__name__)
NAMESPACE = "pending_decision"


class DecisionStateStore:
    """Reconciliation metadata in the same database as the durable intent."""

    @staticmethod
    def _load(audit_id: int) -> dict[str, Any] | None:
        with database_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE namespace = ? AND key = ?",
                (NAMESPACE, str(audit_id)),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _save(audit_id: int, metadata: dict[str, Any]) -> None:
        with database_connection() as connection, connection:
            connection.execute(
                "INSERT INTO runtime_state(namespace,key,value_json,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(namespace,key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (NAMESPACE, str(audit_id), json.dumps(metadata, ensure_ascii=False), str(time.time())),
            )

    async def load(self, audit_id: int) -> dict[str, Any] | None:
        return await run_storage(self._load, audit_id)

    async def save(self, audit_id: int, metadata: dict[str, Any]) -> None:
        await run_storage(self._save, audit_id, metadata)

    @staticmethod
    def _prepare(audit_id: int, metadata: dict[str, Any]) -> bool:
        with database_connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            # The oldest durable pending intent wins across service instances.
            # A second process cannot race a different request for the same UID.
            conflict = connection.execute(
                "SELECT id FROM join_request_audits WHERE action_status='pending' AND id < ? "
                "AND (qq = ? OR (? IS NOT NULL AND bili_uid = ?)) LIMIT 1",
                (audit_id, metadata["qq"], metadata.get("bili_uid"), metadata.get("bili_uid")),
            ).fetchone()
            if conflict is not None:
                return False
            if (metadata["expected_result"] == "approved" and metadata.get("bili_uid") is not None
                    and metadata.get('enforce_uid_owner', True)):
                owner = connection.execute(
                    "SELECT owner_qq FROM bili_bindings WHERE bili_uid=?", (metadata["bili_uid"],),
                ).fetchone()
                if owner is not None and str(owner[0]) != metadata["qq"]:
                    return False
            connection.execute(
                "INSERT INTO runtime_state(namespace,key,value_json,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(namespace,key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (NAMESPACE, str(audit_id), json.dumps(metadata, ensure_ascii=False), str(time.time())),
            )
            return True

    async def prepare(self, audit_id: int, metadata: dict[str, Any]) -> bool:
        return await run_storage(self._prepare, audit_id, metadata)


@dataclass(frozen=True)
class RequestEvidence:
    """Request-specific inspector result; membership is never sufficient evidence.

    ``expected_result`` names the exact requested action, not membership state.
    An inspector must identify bot, group, applicant, subtype and flag itself.
    """

    state: str = "unknown"  # applied / unhandled / unavailable / unknown
    bot_id: str = ""
    group_id: str = ""
    qq: str = ""
    sub_type: str = ""
    flag: str = ""
    expected_result: str = ""
    detail: str = ""

    def matches(self, metadata: dict[str, Any]) -> bool:
        fields = ("bot_id", "group_id", "qq", "sub_type", "flag", "expected_result")
        return all(str(getattr(self, field)) == str(metadata.get(field, "")) for field in fields)


def _scoped_keys(qq: str, bili_uid: int | None, request_key: str = "", audit_id: int | None = None) -> tuple[str, ...]:
    # A separate namespace permits callers to hold the shared legacy identity
    # locks throughout reads/review/execute without re-entering those locks.
    keys = [f"scoped:qq:{qq}"]
    if bili_uid is not None:
        keys.append(f"scoped:uid:{bili_uid}")
    if request_key:
        keys.append(f"request:{request_key}")
    if audit_id is not None:
        keys.append(f"audit:{audit_id}")
    return tuple(keys)


class ScopedDecisionService(DecisionService):
    """Keep uncertain outcomes pending while unrelated identities can proceed.

    Intent still precedes every external action. Pending audit rows themselves
    reserve QQ, UID and request across restarts; metadata is only used to replay
    an exact known action after explicit still-unhandled evidence.
    """

    def __init__(
        self,
        storage: DecisionStorage,
        *,
        locks: DecisionLocks | None = None,
        state_store: DecisionStateStore | None = None,
        retry_delay: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(storage, locks=locks)
        self.state_store = state_store if state_store is not None else DecisionStateStore()
        self.retry_delay = max(0.0, retry_delay)
        self.clock = clock

    async def pending_snapshot(self) -> list[DecisionAudit]:
        count = await self.storage.get_pending_decision_count()
        return list(await self.storage.get_pending_decisions(max(20, count)))

    async def has_pending_identity(self, qq: str, bili_uid: int | None = None) -> bool:
        return any(
            audit.qq == str(qq) or (bili_uid is not None and audit.bili_uid == bili_uid)
            for audit in await self.pending_snapshot()
        )

    @staticmethod
    def _metadata(
        request: DecisionRequest, audit_id: int, kwargs: dict[str, Any], params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        expected = kwargs.get("result", "")
        approve = expected == "approved"
        action = dict(params) if params is not None else {
            "flag": request.flag, "sub_type": request.sub_type, "approve": approve,
            "reason": "" if approve else build_reject_reason(kwargs.get("reasons", [])),
        }
        if expected not in {"approved", "rejected"}:
            raise ValueError("Only an approved/rejected audit can execute a QQ action")
        if (
            action.get("flag") != request.flag or action.get("sub_type") != request.sub_type
            or type(action.get("approve")) is not bool or action["approve"] != approve
            or set(action) - {"flag", "sub_type", "approve", "reason"}
            or not isinstance(action.get("reason", ""), str)
            or str(kwargs.get("group_id", "")) != request.group_id
        ):
            raise ValueError("Action metadata does not match the persisted request intent")
        return {
            "version": 1, "audit_id": audit_id, "bot_id": request.bot_id,
            "group_id": request.group_id, "qq": str(kwargs["qq"]), "bili_uid": kwargs.get("bili_uid"),
            "sub_type": request.sub_type, "flag": request.flag, "request_key": request.request_key,
            "expected_result": expected, "action": "set_group_add_request", "params": action,
            "phase": "prepared", "retry_count": 0, "reconcile_attempts": 0, "next_retry_at": 0.0,
            "inspection_attempts": 0, "next_inspection_at": 0.0,
            # Preserve explicit pipeline policy. Recheck a previously true
            # no_conflict fact atomically, without inventing that condition for
            # pipelines which omit it or deliberately accept its false branch.
            "enforce_uid_owner": (kwargs.get('condition_states', {}).get('no_conflict') == 'true'
                                  if 'condition_states' in kwargs else True),
        }

    async def execute(
        self,
        request: DecisionRequest,
        *,
        audit_kwargs: dict[str, Any],
        perform_action: Callable[[], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]],
        action_params: dict[str, Any] | None = None,
    ) -> bool:
        qq, uid = str(audit_kwargs.get("qq", "")), audit_kwargs.get("bili_uid")
        async with self.locks.hold(*_scoped_keys(qq, uid, request.request_key)):
            if self.paused:
                await notify("自动审批因本地存储异常暂停，请管理员检查数据库后恢复。")
                return False
            pending = await self.pending_snapshot()
            related = next((audit for audit in pending if (
                audit.qq == qq or (uid is not None and audit.bili_uid == uid)
                or (request.request_key and getattr(audit, "request_key", "") == request.request_key)
            )), None)
            if related is not None:
                await notify(f"该QQ、B站UID或申请存在待确认审批 #{related.id}，本次保持未处理；其他用户审批继续。")
                return False
            # Validate caller metadata before creating the durable pending row.
            metadata = self._metadata(request, 0, audit_kwargs, action_params)
            audit = await self.storage.begin_pending_decision(**audit_kwargs, request_key=request.request_key)
            if audit is None or audit.id is None:
                self.paused = True
                await notify("审批前置审计写入失败，自动审批已暂停，请检查数据库。")
                return False
            if not audit.was_created:
                return audit.action_status == "applied" and audit.qq == qq and audit.bili_uid == uid
            metadata["audit_id"] = audit.id
            metadata["phase"] = "dispatching"
            try:
                reserved = await self.state_store.prepare(audit.id, metadata)
            except Exception:
                logger.exception("Exact request metadata could not be persisted; no QQ action executed")
                self.paused = True
                await notify(f"审批 #{audit.id} 元数据写入失败，未执行QQ动作，请检查数据库并人工确认该记录。")
                return False
            if not reserved:
                await self.storage.finalize_pending_decision(
                    audit.id, applied=False, failure_reason="相同QQ或B站UID已有先行审批或归属冲突，本次未执行QQ动作"
                )
                await notify(f"审批 #{audit.id} 因并发身份预约冲突未执行QQ动作，请重新核实该账号。")
                return False
            return await self._dispatch(audit.id, metadata, perform_action, notify)

    async def _save_phase(self, audit_id: int, metadata: dict[str, Any], phase: str, detail: str = "") -> None:
        metadata = {**metadata, "phase": phase, "detail": detail}
        try:
            await self.state_store.save(audit_id, metadata)
        except Exception:
            # The original pending audit remains the durable identity reservation.
            logger.exception("Could not update reconciliation metadata for audit %s", audit_id)

    async def _dispatch(
        self, audit_id: int, metadata: dict[str, Any], perform_action: Callable[[], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]],
    ) -> bool:
        try:
            await perform_action()
        except DefiniteActionFailure as exc:
            if int(metadata.get('retry_count', 0)):
                # A rejected replay says nothing definitive about the earlier
                # timed-out call: it may complete after the unhandled snapshot.
                # Keep its identity reservation until exact/manual resolution.
                await self._save_phase(audit_id, metadata, 'manual', 'replay_rejected:' + exc.error_type)
                await notify(f'审批 #{audit_id} 的重发被QQ接口拒绝，原操作结果仍未确认；已保留身份占用，请人工核实。')
                return False
            finalized = await self.storage.finalize_pending_decision(
                audit_id, applied=False, failure_reason=f"QQ审批接口明确失败，申请保持未处理：{exc.error_type}"
            )
            await self._save_phase(audit_id, metadata, "failed" if finalized else "manual", exc.error_type)
            if not finalized:
                await notify(f"审批 #{audit_id} 的失败结果未能保存，请人工核实该记录。")
            return False
        except asyncio.CancelledError:
            metadata["next_retry_at"] = self.clock() + self.retry_delay
            await self._save_phase(audit_id, metadata, "uncertain", "action_cancelled")
            raise
        except Exception as exc:
            metadata["next_retry_at"] = self.clock() + self.retry_delay
            await self._save_phase(audit_id, metadata, "uncertain", type(exc).__name__)
            await notify(f"审批 #{audit_id} 结果暂无法确认（{type(exc).__name__}），仅隔离该QQ、UID和申请；请等待精确对账或人工确认。")
            return False
        finalized = await self.storage.finalize_pending_decision(audit_id, applied=True)
        await self._save_phase(audit_id, metadata, "applied" if finalized else "manual")
        if not finalized:
            await notify(f"QQ审批已执行，但审计 #{audit_id} 仍未确认；请勿重复审批，并人工核对该记录。")
        return finalized

    async def restore_pending(self) -> int:
        # A fresh snapshot restores reservations without imposing a global pause.
        pending = await self.pending_snapshot()
        self.paused = False
        return len(pending)

    async def resolve_pending(self, audit_id: int, *, applied: bool) -> ResolutionResult:
        audit = next((item for item in await self.pending_snapshot() if item.id == audit_id), None)
        if audit is None:
            return ResolutionResult(found=False)
        async with self.locks.hold(
            *_scoped_keys(audit.qq, audit.bili_uid, getattr(audit, "request_key", ""), audit_id),
            *identity_decision_keys(audit.qq, audit.bili_uid),
        ):
            # Manual resolution and automatic reconciliation use the same locks;
            # the storage transaction still provides the final once-only guard.
            if not any(item.id == audit_id for item in await self.pending_snapshot()):
                return ResolutionResult(found=False)
            finalized = await self.storage.finalize_pending_decision(
                audit_id, applied=applied, failure_reason="超级管理员确认 QQ 审批未生效"
            )
            if finalized:
                metadata = await self.state_store.load(audit_id)
                if metadata is not None:
                    await self._save_phase(audit_id, metadata, "applied" if applied else "failed", "manual_confirmation")
            return ResolutionResult(found=True, finalized=finalized, remaining=await self.storage.get_pending_decision_count())

    @staticmethod
    def _valid_metadata(metadata: dict[str, Any] | None, audit: DecisionAudit) -> bool:
        if not metadata or metadata.get("version") != 1:
            return False
        required = ("bot_id", "group_id", "qq", "sub_type", "flag", "request_key")
        if not all(isinstance(metadata.get(key), str) and metadata[key].strip() for key in required):
            return False
        if (
            metadata.get("audit_id") != audit.id or metadata["qq"] != audit.qq
            or metadata.get("bili_uid") != audit.bili_uid
            or metadata["request_key"] != getattr(audit, "request_key", "")
            or metadata.get("group_id") != getattr(audit, "group_id", metadata.get("group_id"))
            or metadata.get("expected_result") != getattr(audit, "result", metadata.get("expected_result"))
            or metadata.get("expected_result") not in {"approved", "rejected"}
            or metadata.get("action") != "set_group_add_request"
        ):
            return False
        request = DecisionRequest(metadata["bot_id"], metadata["group_id"], metadata["sub_type"], metadata["flag"])
        if request.request_key != metadata["request_key"]:
            return False
        params = metadata.get("params")
        return isinstance(params, dict) and (
            params.get("flag") == metadata["flag"] and params.get("sub_type") == metadata["sub_type"]
            and type(params.get("approve")) is bool
            and params["approve"] == (metadata["expected_result"] == "approved")
            and isinstance(params.get("reason", ""), str)
            and not set(params) - {"flag", "sub_type", "approve", "reason"}
        )

    async def reconcile_pending(
        self, audit_id: int, *,
        inspector: Callable[[dict[str, Any]], Awaitable[RequestEvidence]],
        replay: Callable[[dict[str, Any]], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]],
    ) -> str:
        audit = next((item for item in await self.pending_snapshot() if item.id == audit_id), None)
        if audit is None:
            return "not_pending"
        async with self.locks.hold(
            *_scoped_keys(audit.qq, audit.bili_uid, getattr(audit, "request_key", ""), audit_id),
            *identity_decision_keys(audit.qq, audit.bili_uid),
        ):
            if not any(item.id == audit_id for item in await self.pending_snapshot()):
                return "not_pending"
            metadata = await self.state_store.load(audit_id)
            if not self._valid_metadata(metadata, audit):
                # Old audit rows contain no trustworthy action arguments.
                return "manual"
            assert metadata is not None
            if metadata.get("phase") == "manual":
                return "manual"
            if self.clock() < float(metadata.get("next_inspection_at", 0.0)):
                return "backoff"
            metadata["reconcile_attempts"] = int(metadata.get("reconcile_attempts", 0)) + 1
            try:
                evidence = await inspector(copy.deepcopy(metadata))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._save_phase(audit_id, metadata, "manual", f"inspector:{type(exc).__name__}")
                return "manual"
            if isinstance(evidence, RequestEvidence) and evidence.state == "unavailable":
                # A recognized disconnected/timeout inspector is distinct from
                # an available endpoint returning no exact request evidence.
                # These retries read status only and never authorize an action.
                attempts = int(metadata.get("inspection_attempts", 0)) + 1
                metadata["inspection_attempts"] = attempts
                delays = (5.0, 15.0, 45.0)
                if attempts >= 4:
                    await self._save_phase(audit_id, metadata, "manual", "inspection_unavailable_budget_exhausted")
                    await notify(f"审批 #{audit_id} 的精确对账接口连续4次不可用，已保留待人工确认；其他用户审批继续。")
                    return "manual"
                metadata["next_inspection_at"] = self.clock() + delays[attempts - 1]
                await self.state_store.save(audit_id, {**metadata, "phase": "inspection_backoff", "detail": evidence.detail})
                return "backoff"
            if not isinstance(evidence, RequestEvidence) or not evidence.matches(metadata):
                await self._save_phase(audit_id, metadata, "manual", "no_exact_request_evidence")
                return "manual"
            metadata["inspection_attempts"] = 0
            metadata["next_inspection_at"] = 0.0
            if evidence.state == "applied":
                finalized = await self.storage.finalize_pending_decision(audit_id, applied=True)
                await self._save_phase(audit_id, metadata, "applied" if finalized else "manual", evidence.detail)
                return "applied" if finalized else "manual"
            if evidence.state != "unhandled":
                await self._save_phase(audit_id, metadata, "manual", evidence.detail or "unknown_request_state")
                return "manual"
            if int(metadata.get("retry_count", 0)) >= 1:
                await self._save_phase(audit_id, metadata, "manual", "bounded_retry_exhausted")
                return "manual"
            if self.clock() < float(metadata.get("next_retry_at", 0)):
                await self.state_store.save(audit_id, metadata)
                return "backoff"
            metadata.update(retry_count=1, phase="retrying", next_retry_at=self.clock() + self.retry_delay * 2)
            # Saving the retry budget precedes replay, including cancellation.
            await self.state_store.save(audit_id, metadata)
            applied = await self._dispatch(audit_id, metadata, lambda: replay(copy.deepcopy(metadata)), notify)
            if applied:
                return "applied"
            return "pending" if any(item.id == audit_id for item in await self.pending_snapshot()) else "failed"
