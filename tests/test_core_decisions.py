"""Durable decision semantics without importing a framework or SQLite adapter."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock

from src.gatekeeper.decision_runtime import DecisionLocks, identity_decision_keys
from src.gatekeeper.decisions import DecisionRequest, DecisionService, DefiniteActionFailure, ResolutionResult


@dataclass
class Audit:
    id: int | None
    qq: str = "10001"
    bili_uid: int | None = 888
    was_created: bool = True
    action_status: str = "pending"
    request_key: str = ""
    result: str = "approved"
    failure_reason: str = ""


class MemoryStorage:
    """Represent durable intent and deduplication, leaving scheduling to the action."""

    def __init__(self):
        self.audits = {}
        self.calls = []
        self.ignored = []
        self.fail_begin = False
        self.missing_id = False
        self.fail_add = False
        self.fail_finalize = False

    async def add_audit(self, **kwargs):
        self.calls.append(("add",))
        self.ignored.append(kwargs)
        return None if self.fail_add else Audit(id=100, action_status="not_required")

    async def begin_pending_decision(self, **kwargs):
        self.calls.append(("begin", kwargs["request_key"]))
        if self.fail_begin:
            return None
        if self.missing_id:
            return Audit(id=None)
        request_key = kwargs["request_key"]
        if request_key:
            existing = next((audit for audit in self.audits.values() if audit.request_key == request_key), None)
            if existing is not None:
                return replace(existing, was_created=False)
        audit_id = len(self.audits) + 1
        audit = Audit(
            id=audit_id,
            request_key=request_key,
            qq=kwargs["qq"],
            bili_uid=kwargs.get("bili_uid"),
            result=kwargs["result"],
        )
        self.audits[audit_id] = audit
        return audit

    async def finalize_pending_decision(self, audit_id, *, applied, failure_reason=""):
        self.calls.append(("finalize", audit_id, applied, failure_reason))
        if self.fail_finalize:
            return False
        audit = self.audits.get(audit_id)
        if audit is None:
            return False
        if audit.action_status != "pending":
            return audit.action_status == ("applied" if applied else "failed")
        audit.action_status = "applied" if applied else "failed"
        audit.failure_reason = "" if applied else failure_reason
        if not applied:
            audit.result = "ignored"
        return True

    async def get_pending_decision_count(self):
        self.calls.append(("count",))
        return sum(audit.action_status == "pending" for audit in self.audits.values())

    async def get_pending_decisions(self, limit=20):
        self.calls.append(("pending", limit))
        return [audit for audit in self.audits.values() if audit.action_status == "pending"][:limit]


def audit_kwargs(*, qq="10001", uid=888):
    return dict(group_id="90001", qq=qq, bili_uid=uid, result="approved", reasons=["通过"])


class DecisionRequestTests(unittest.TestCase):
    def test_request_key_includes_bot_group_and_subtype_and_trims_flag(self):
        self.assertEqual(DecisionRequest("90000", "90001", "add", " flag ").request_key, "90000:90001:add:flag")
        for field in ("bot_id", "group_id", "sub_type", "flag"):
            with self.subTest(field=field):
                request = DecisionRequest("90000", "90001", "add", "flag")
                self.assertNotEqual(request.request_key, replace(request, **{field: "different"}).request_key)

    def test_blank_flags_keep_deduplication_disabled(self):
        for flag in ("", " ", "\t\n"):
            with self.subTest(flag=flag):
                self.assertEqual(DecisionRequest("90000", "90001", "add", flag).request_key, "")


class DecisionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.service = DecisionService(self.storage)
        self.action = AsyncMock()
        self.notify = AsyncMock()
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    async def execute(self, flag="first", *, action=None, kwargs=None):
        return await self.service.execute(
            DecisionRequest("90000", "90001", "add", flag),
            audit_kwargs=kwargs if kwargs is not None else audit_kwargs(),
            perform_action=action if action is not None else self.action,
            notify=self.notify,
        )

    async def create_pending(self, flag="first", *, qq="10001", uid=888):
        return await self.storage.begin_pending_decision(
            **audit_kwargs(qq=qq, uid=uid), request_key=DecisionRequest("90000", "90001", "add", flag).request_key
        )

    async def test_persists_intent_then_action_then_finalization(self):
        async def action():
            self.assertEqual(self.storage.audits[1].action_status, "pending")
            self.storage.calls.append(("action",))

        self.assertTrue(await self.execute(action=action))
        self.assertEqual([call[0] for call in self.storage.calls], ["begin", "action", "finalize"])
        self.assertEqual(self.storage.audits[1].action_status, "applied")
        self.assertFalse(self.service.paused)
        self.assertEqual(self.service.locks._entries, {})
        self.notify.assert_not_awaited()

    async def test_paused_service_records_ignore_without_mutating_input(self):
        self.service.paused = True
        kwargs = audit_kwargs()
        self.assertFalse(await self.execute(kwargs=kwargs))
        self.assertEqual([call[0] for call in self.storage.calls], ["add"])
        self.assertEqual(self.storage.ignored[0]["result"], "ignored")
        self.assertEqual(self.storage.ignored[0]["reasons"], ["存在尚未人工确认的历史审批，自动审批已暂停"])
        self.assertEqual(kwargs["result"], "approved")
        self.assertEqual(kwargs["reasons"], ["通过"])
        self.action.assert_not_awaited()
        self.notify.assert_awaited_once()

    async def test_paused_ignore_write_failure_still_blocks_action(self):
        self.service.paused = True
        self.storage.fail_add = True
        with self.assertLogs("src.gatekeeper.decisions", level="CRITICAL"):
            self.assertFalse(await self.execute())
        self.assertTrue(self.service.paused)
        self.action.assert_not_awaited()
        self.notify.assert_awaited_once()

    async def test_missing_pre_action_audit_pauses_without_calling_action(self):
        for field in ("fail_begin", "missing_id"):
            with self.subTest(field=field):
                self.storage = MemoryStorage()
                setattr(self.storage, field, True)
                self.service = DecisionService(self.storage)
                self.notify.reset_mock()
                with self.assertLogs("src.gatekeeper.decisions", level="CRITICAL"):
                    self.assertFalse(await self.execute())
                self.assertTrue(self.service.paused)
                self.action.assert_not_awaited()
                self.assertIn("10001", self.notify.await_args.args[0])
                self.assertFalse(any(call[0] == "finalize" for call in self.storage.calls))

    async def test_definite_failure_finalizes_failed_and_preserves_error_label(self):
        self.action.side_effect = DefiniteActionFailure("ActionFailed")
        with self.assertLogs("src.gatekeeper.decisions", level="ERROR"):
            self.assertFalse(await self.execute())
        audit = self.storage.audits[1]
        self.assertEqual(audit.action_status, "failed")
        self.assertEqual(audit.result, "ignored")
        self.assertEqual(audit.failure_reason, "调用加群审批接口失败，申请保持未处理状态：ActionFailed")
        self.assertFalse(self.service.paused)
        self.action.assert_awaited_once()
        self.notify.assert_not_awaited()

    async def test_definite_failure_with_failed_finalization_leaves_pending_and_pauses(self):
        self.action.side_effect = DefiniteActionFailure("ActionFailed")
        self.storage.fail_finalize = True
        with self.assertLogs("src.gatekeeper.decisions", level="ERROR"):
            self.assertFalse(await self.execute())
        self.assertEqual(self.storage.audits[1].action_status, "pending")
        self.assertTrue(self.service.paused)
        self.assertIn("接口失败后无法更新审计 #1", self.notify.await_args.args[0])

    async def test_unknown_action_result_leaves_pending_and_pauses_without_retry(self):
        for error in (TimeoutError("lost response"), ConnectionError("disconnected"), RuntimeError("unknown")):
            with self.subTest(error=type(error).__name__):
                self.storage = MemoryStorage()
                self.service = DecisionService(self.storage)
                self.action = AsyncMock(side_effect=error)
                self.notify.reset_mock()
                with self.assertLogs("src.gatekeeper.decisions", level="ERROR"):
                    self.assertFalse(await self.execute())
                self.assertTrue(self.service.paused)
                self.assertEqual(self.storage.audits[1].action_status, "pending")
                self.assertEqual([call[0] for call in self.storage.calls], ["begin"])
                self.action.assert_awaited_once()
                self.assertIn(type(error).__name__, self.notify.await_args.args[0])
                self.assertEqual(self.service.locks._entries, {})

    async def test_cancelled_action_leaves_pending_and_rethrows_cancellation(self):
        started = asyncio.Event()

        async def action():
            started.set()
            await asyncio.Event().wait()

        task = self.start(self.execute(action=action))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertLogs("src.gatekeeper.decisions", level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.service.paused)
        self.assertEqual(self.storage.audits[1].action_status, "pending")
        self.assertEqual([call[0] for call in self.storage.calls], ["begin"])
        self.assertEqual(self.service.locks._entries, {})
        self.notify.assert_not_awaited()

    async def test_completed_action_with_failed_finalization_stays_pending_and_pauses(self):
        self.storage.fail_finalize = True
        with self.assertLogs("src.gatekeeper.decisions", level="CRITICAL"):
            self.assertFalse(await self.execute())
        self.assertTrue(self.service.paused)
        self.assertEqual(self.storage.audits[1].action_status, "pending")
        self.action.assert_awaited_once()
        self.assertIn("请勿重复审批", self.notify.await_args.args[0])

    async def test_completed_duplicate_succeeds_without_second_action(self):
        self.assertTrue(await self.execute())
        self.assertTrue(await self.execute())
        self.action.assert_awaited_once()
        self.assertFalse(self.service.paused)
        self.assertEqual(len(self.storage.audits), 1)
        self.assertEqual(sum(call[0] == "finalize" for call in self.storage.calls), 1)

    async def test_orphan_pending_duplicate_pauses_without_action(self):
        await self.create_pending()
        self.assertFalse(await self.execute())
        self.assertTrue(self.service.paused)
        self.assertEqual(len(self.storage.audits), 1)
        self.action.assert_not_awaited()
        self.assertIn("重复的未确认审批 #1", self.notify.await_args.args[0])

    async def test_failed_duplicate_stays_failed_without_action_or_pause(self):
        audit = await self.create_pending()
        await self.storage.finalize_pending_decision(audit.id, applied=False)
        with self.assertLogs("src.gatekeeper.decisions", level="WARNING"):
            self.assertFalse(await self.execute())
        self.assertFalse(self.service.paused)
        self.action.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_in_flight_duplicate_waits_for_original_and_only_runs_action_once(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def action():
            started.set()
            await release.wait()

        self.action.side_effect = action
        first = self.start(self.execute())
        await asyncio.wait_for(started.wait(), timeout=2)
        duplicate = self.start(self.execute())
        await asyncio.sleep(0)
        self.assertFalse(duplicate.done())
        self.assertFalse(self.service.paused)
        self.assertEqual(len(self.storage.audits), 1)
        release.set()
        self.assertEqual(await asyncio.wait_for(asyncio.gather(first, duplicate), timeout=2), [True, True])
        self.action.assert_awaited_once()
        self.assertFalse(self.service.paused)
        self.assertEqual(self.service.locks._entries, {})

    async def test_different_request_can_complete_while_another_waits(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def action():
            started.set()
            await release.wait()

        first = self.start(self.execute(action=action))
        await asyncio.wait_for(started.wait(), timeout=2)
        self.assertTrue(await asyncio.wait_for(self.execute("second"), timeout=2))
        self.assertFalse(first.done())
        release.set()
        self.assertTrue(await first)

    async def test_blank_flags_do_not_deduplicate_distinct_events(self):
        self.assertTrue(await self.execute(""))
        self.assertTrue(await self.execute(" "))
        self.assertEqual(len(self.storage.audits), 2)
        self.assertEqual(self.action.await_count, 2)

    async def test_restore_pending_pauses_on_durable_intent_and_clears_when_empty(self):
        audit = await self.create_pending()
        with self.assertLogs("src.gatekeeper.decisions", level="CRITICAL"):
            self.assertEqual(await self.service.restore_pending(), 1)
        self.assertTrue(self.service.paused)
        self.action.assert_not_awaited()
        await self.storage.finalize_pending_decision(audit.id, applied=True)
        self.assertEqual(await self.service.restore_pending(), 0)
        self.assertFalse(self.service.paused)

    async def test_missing_manual_resolution_does_not_change_pause(self):
        self.service.paused = True
        result = await self.service.resolve_pending(99, applied=True)
        self.assertEqual(result, ResolutionResult(found=False))
        self.assertTrue(self.service.paused)
        self.assertEqual(self.storage.calls, [("pending", 1000)])

    async def test_failed_manual_finalization_preserves_pause(self):
        audit = await self.create_pending()
        self.service.paused = True
        self.storage.fail_finalize = True
        result = await self.service.resolve_pending(audit.id, applied=True)
        self.assertEqual(result, ResolutionResult(found=True))
        self.assertTrue(self.service.paused)
        self.assertEqual(audit.action_status, "pending")
        self.assertFalse(any(call[0] == "count" for call in self.storage.calls))

    async def test_manual_resolution_recounts_and_resumes_after_last_pending(self):
        first = await self.create_pending("first")
        second = await self.create_pending("second", qq="10002", uid=889)
        self.service.paused = True
        first_result = await self.service.resolve_pending(first.id, applied=True)
        self.assertEqual(first_result, ResolutionResult(found=True, finalized=True, remaining=1))
        self.assertTrue(self.service.paused)
        self.assertEqual(first.action_status, "applied")
        last_result = await self.service.resolve_pending(second.id, applied=False)
        self.assertEqual(last_result, ResolutionResult(found=True, finalized=True, remaining=0))
        self.assertEqual(second.action_status, "failed")
        self.assertEqual(second.failure_reason, "超级管理员确认 QQ 审批未生效")
        self.assertFalse(self.service.paused)
        self.assertEqual(self.service.locks._entries, {})

    async def test_manual_resolution_shares_identity_locks_with_live_reviews(self):
        for key in identity_decision_keys("10001", 888):
            with self.subTest(key=key):
                self.storage = MemoryStorage()
                locks = DecisionLocks()
                self.service = DecisionService(self.storage, locks=locks)
                audit = await self.create_pending()
                self.assertIs(self.service.locks, locks)
                async with locks.hold(key):
                    resolving = self.start(self.service.resolve_pending(audit.id, applied=True))
                    await asyncio.sleep(0)
                    self.assertFalse(resolving.done())
                    self.assertEqual(audit.action_status, "pending")
                result = await asyncio.wait_for(resolving, timeout=2)
                self.assertTrue(result.finalized)
                self.assertEqual(locks._entries, {})

    async def test_manual_resolution_releases_identity_locks_before_recount(self):
        audit = await self.create_pending()
        original_count = self.storage.get_pending_decision_count

        async def count():
            for key in identity_decision_keys(audit.qq, audit.bili_uid):
                self.assertNotIn(key, self.service.locks._entries)
            return await original_count()

        self.storage.get_pending_decision_count = count
        result = await self.service.resolve_pending(audit.id, applied=True)
        self.assertTrue(result.finalized)


class DecisionLocksTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_keys_include_qq_and_optional_uid(self):
        self.assertEqual(identity_decision_keys("123", None), ("identity:qq:123",))
        self.assertEqual(identity_decision_keys("123", 456), ("identity:qq:123", "identity:uid:456"))

    async def test_duplicate_and_blank_keys_do_not_deadlock_and_are_cleaned(self):
        locks = DecisionLocks()
        async with locks.hold("", "same", "same"):
            self.assertEqual(set(locks._entries), {"same"})
        self.assertEqual(locks._entries, {})

    async def test_cancelled_waiter_releases_partially_acquired_keys(self):
        locks = DecisionLocks()

        async def waiter():
            async with locks.hold("a", "b"):
                self.fail("waiter must remain blocked")

        async with locks.hold("b"):
            task = asyncio.create_task(waiter())
            try:
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(set(locks._entries), {"b"})
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(locks._entries, {})


if __name__ == "__main__":
    unittest.main()
