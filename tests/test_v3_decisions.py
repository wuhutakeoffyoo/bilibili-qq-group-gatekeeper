"""Real SQLite tests for scoped pending reservations and exact reconciliation."""

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.database import legacy
from app.database.async_storage import run_storage
from app.database.records import JoinRequestRecordManager as Records
from app.gatekeeper.decisions import DecisionStateStore, RequestEvidence, ScopedDecisionService
from app.gatekeeper.reconciler import DecisionReconciler
from src.gatekeeper.decisions import DecisionRequest, DefiniteActionFailure


class SqliteStorage:
    async def add_audit(self, **kwargs):
        return await run_storage(Records.add_audit, **kwargs)

    async def begin_pending_decision(self, **kwargs):
        return await run_storage(Records.begin_pending_decision, **kwargs)

    async def finalize_pending_decision(self, audit_id, **kwargs):
        return await run_storage(Records.finalize_pending_decision, audit_id, **kwargs)

    async def get_pending_decision_count(self):
        return await run_storage(Records.get_pending_decision_count)

    async def get_pending_decisions(self, limit=20):
        return await run_storage(Records.get_pending_decisions, limit)


def evidence(metadata, state="applied", **changes):
    fields = {key: metadata[key] for key in ("bot_id", "group_id", "qq", "sub_type", "flag", "expected_result")}
    fields.update(changes)
    return RequestEvidence(state=state, **fields)


class ScopedDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for attr, name in (
            ("DATABASE_FILE", "records.sqlite3"), ("LEGACY_JOIN_REQUEST_FILE", "absent.json"),
            ("LEGACY_LEAVE_RECORDS_PATH", "absent-leaves"), ("VERY_LEGACY_LEAVE_RECORDS_PATH", "absent-old-leaves"),
        ):
            self.stack.enter_context(patch.object(legacy, attr, root / name))
        legacy.reset_database_state()
        self.addCleanup(legacy.reset_database_state)
        self.storage = SqliteStorage()
        self.now = 1000.0
        self.service = ScopedDecisionService(self.storage, clock=lambda: self.now, retry_delay=10)
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

    async def execute(self, flag="one", qq="10001", uid=88, action=None, service=None, result="approved"):
        request = DecisionRequest("90000", "90001", "add", flag)
        return await (service or self.service).execute(
            request, audit_kwargs=dict(group_id="90001", qq=qq, bili_uid=uid, result=result, reasons=["审核理由"]),
            perform_action=action if action is not None else AsyncMock(), notify=self.notify,
            action_params=dict(flag=flag, sub_type="add", approve=result == "approved", reason="" if result == "approved" else "准确拒绝原因"),
        )

    async def pending(self, flag="one", qq="10001", uid=88, result="approved"):
        action = AsyncMock(side_effect=TimeoutError("response lost"))
        self.assertFalse(await self.execute(flag=flag, qq=qq, uid=uid, result=result, action=action))
        return (await self.storage.get_pending_decisions(100))[-1]

    async def test_intent_and_exact_payload_precede_action_and_binding_applies_once(self):
        async def action():
            audits = await self.service.pending_snapshot()
            self.assertEqual(len(audits), 1)
            self.assertIsNone(Records.get_bili_binding(88))
            metadata = await self.service.state_store.load(audits[0].id)
            self.assertEqual(metadata["phase"], "dispatching")
            self.assertEqual(metadata["params"], {"flag": "one", "sub_type": "add", "approve": True, "reason": ""})

        self.assertTrue(await self.execute(action=action))
        duplicate = AsyncMock()
        self.assertTrue(await self.execute(action=duplicate))
        duplicate.assert_not_awaited()
        self.assertEqual(Records.get_user_binding("10001").approved_count, 1)
        self.assertEqual(Records.get_bili_binding(88).owner_qq, "10001")

    async def test_uncertain_only_reserves_related_qq_uid_and_request(self):
        await self.pending()
        self.assertFalse(self.service.paused)
        self.assertTrue(await self.execute(flag="other", qq="10002", uid=89))
        blocked = AsyncMock()
        self.assertFalse(await self.execute(flag="same-qq", uid=90, action=blocked))
        self.assertFalse(await self.execute(flag="same-uid", qq="10003", action=blocked))
        blocked.assert_not_awaited()
        self.assertEqual(await self.storage.get_pending_decision_count(), 1)

    async def test_restart_restores_pending_reservations_without_global_pause(self):
        audit = await self.pending()
        restored = ScopedDecisionService(self.storage)
        self.assertEqual(await restored.restore_pending(), 1)
        self.assertFalse(restored.paused)
        self.assertTrue(await restored.has_pending_identity("other", 88))
        self.assertFalse(await self.execute(service=restored))
        self.assertTrue(await self.execute(flag="new", qq="10002", uid=89, service=restored))
        self.assertEqual((await restored.state_store.load(audit.id))["flag"], "one")

    async def test_legacy_pending_without_exact_metadata_never_guesses_or_replays(self):
        audit = await self.storage.begin_pending_decision(
            group_id="90001", qq="10001", bili_uid=88, result="approved", reasons=[], request_key="legacy:unknown",
        )
        inspector, replay = AsyncMock(), AsyncMock()
        self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspector, replay=replay, notify=self.notify), "manual")
        inspector.assert_not_awaited()
        replay.assert_not_awaited()
        self.assertFalse(await self.execute(flag="blocked"))
        self.assertTrue(await self.execute(flag="free", qq="10002", uid=89))

    async def test_wrong_request_identity_or_result_cannot_finalize(self):
        for index, field in enumerate(("bot_id", "group_id", "qq", "sub_type", "flag", "expected_result")):
            with self.subTest(field=field):
                audit = await self.pending(flag=f"flag-{index}", qq=str(20000 + index), uid=100 + index)

                async def inspect(metadata, changed=field):
                    return evidence(metadata, **{changed: "wrong"})

                replay = AsyncMock()
                self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify), "manual")
                replay.assert_not_awaited()
                self.assertEqual((await self.service.state_store.load(audit.id))["phase"], "manual")
        self.assertEqual(await self.storage.get_pending_decision_count(), 6)

    async def test_membership_only_or_unknown_evidence_requires_manual_confirmation(self):
        audit = await self.pending()
        inspector = AsyncMock(return_value={"state": "applied", "is_member": True})
        replay = AsyncMock()
        self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspector, replay=replay, notify=self.notify), "manual")
        self.assertEqual(await self.storage.get_pending_decision_count(), 1)
        restored = ScopedDecisionService(self.storage)
        self.assertEqual(await restored.reconcile_pending(audit.id, inspector=inspector, replay=replay, notify=self.notify), "manual")
        self.assertEqual(inspector.await_count, 1)
        replay.assert_not_awaited()

    async def test_exact_applied_evidence_finalizes_approval_or_rejection_once(self):
        for index, result in enumerate(("approved", "rejected")):
            audit = await self.pending(flag=result, qq=str(30000 + index), uid=200 + index, result=result)

            async def inspect(metadata):
                return evidence(metadata)

            replay = AsyncMock()
            self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify), "applied")
            self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify), "not_pending")
            replay.assert_not_awaited()
            summary = Records.get_user_summary(str(30000 + index))
            self.assertEqual(summary.approved_count if result == "approved" else summary.rejected_count, 1)

    async def test_explicit_unhandled_only_retries_once_after_backoff_across_restart(self):
        audit = await self.pending()

        async def inspect(metadata):
            return evidence(metadata, "unhandled")

        replay = AsyncMock(side_effect=TimeoutError())
        kwargs = dict(inspector=inspect, replay=replay, notify=self.notify)
        self.assertEqual(await self.service.reconcile_pending(audit.id, **kwargs), "backoff")
        replay.assert_not_awaited()
        self.now += 11
        self.assertEqual(await self.service.reconcile_pending(audit.id, **kwargs), "pending")
        self.assertEqual(replay.await_count, 1)
        self.assertEqual(replay.await_args.args[0]["params"]["flag"], "one")
        self.assertEqual((await self.service.state_store.load(audit.id))["retry_count"], 1)
        restored = ScopedDecisionService(self.storage, clock=lambda: self.now + 100)
        self.assertEqual(await restored.reconcile_pending(audit.id, **kwargs), "manual")
        self.assertEqual(replay.await_count, 1)

    async def test_exact_unhandled_successfully_replays_rejection_payload(self):
        audit = await self.pending(result="rejected")
        self.now += 11

        async def inspect(metadata):
            return evidence(metadata, "unhandled")

        replay = AsyncMock()
        self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify), "applied")
        self.assertEqual(replay.await_args.args[0]["params"], {
            "flag": "one", "sub_type": "add", "approve": False, "reason": "准确拒绝原因",
        })
        self.assertIsNone(Records.get_bili_binding(88))

    async def test_definite_failure_releases_only_its_reservation(self):
        self.assertFalse(await self.execute(action=AsyncMock(side_effect=DefiniteActionFailure("denied"))))
        self.assertEqual(await self.storage.get_pending_decision_count(), 0)
        self.assertFalse(self.service.paused)
        self.assertTrue(await self.execute(flag="second"))

    async def test_rejected_replay_cannot_disprove_the_original_timed_out_action(self):
        audit = await self.pending()
        self.now += 11
        async def inspect(metadata):
            return evidence(metadata, 'unhandled')
        replay = AsyncMock(side_effect=DefiniteActionFailure('No such request'))
        result = await self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify)
        self.assertEqual(result, 'pending')
        self.assertEqual(await self.storage.get_pending_decision_count(), 1)
        self.assertEqual((await self.service.state_store.load(audit.id))['phase'], 'manual')
        self.assertIsNone(Records.get_bili_binding(88))
        self.assertTrue(await self.service.has_pending_identity('10001', 88))

    async def test_metadata_write_failure_prevents_action(self):
        action = AsyncMock()
        with patch.object(DecisionStateStore, "prepare", new=AsyncMock(side_effect=OSError("disk full"))):
            with self.assertLogs("app.gatekeeper.decisions", level="ERROR"):
                self.assertFalse(await self.execute(action=action))
        action.assert_not_awaited()
        self.assertTrue(self.service.paused)
        self.assertEqual(await self.storage.get_pending_decision_count(), 1)

    async def test_cancellation_preserves_pending_without_blocking_other_users(self):
        started = asyncio.Event()

        async def action():
            started.set()
            await asyncio.Event().wait()

        task = self.start(self.execute(action=action))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.paused)
        self.assertEqual(await self.storage.get_pending_decision_count(), 1)
        self.assertTrue(await self.execute(flag="other", qq="10002", uid=89))

    async def test_manual_resolution_races_reconciliation_without_duplicate_binding(self):
        audit = await self.pending()
        started, release = asyncio.Event(), asyncio.Event()

        async def inspect(metadata):
            started.set()
            await release.wait()
            return evidence(metadata)

        automatic = self.start(self.service.reconcile_pending(audit.id, inspector=inspect, replay=AsyncMock(), notify=self.notify))
        await asyncio.wait_for(started.wait(), 2)
        manual = self.start(self.service.resolve_pending(audit.id, applied=True))
        await asyncio.sleep(0)
        self.assertFalse(manual.done())
        release.set()
        self.assertEqual(await automatic, "applied")
        self.assertFalse((await manual).found)
        self.assertEqual(Records.get_user_binding("10001").approved_count, 1)

    async def test_reconciler_shutdown_cancels_inspection_without_external_action(self):
        await self.pending()
        started = asyncio.Event()

        async def inspect(metadata):
            started.set()
            await asyncio.Event().wait()

        replay = AsyncMock()
        loop = DecisionReconciler(self.service, inspector=inspect, replay=replay, interval=0.01)
        loop.start()
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(loop.stop(), 2)
        self.assertEqual(await self.storage.get_pending_decision_count(), 1)
        replay.assert_not_awaited()

    async def test_untrusted_inspector_cannot_mutate_saved_replay_payload(self):
        audit = await self.pending(result="rejected")
        self.now += 11

        async def inspect(metadata):
            metadata["params"]["approve"] = True
            return evidence(metadata, "unhandled")

        replay = AsyncMock()
        self.assertEqual(await self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify), "applied")
        self.assertFalse(replay.await_args.args[0]["params"]["approve"])

    async def test_independent_instances_cannot_race_two_qqs_for_same_uid(self):
        gate = asyncio.Event()

        class RacingStorage(SqliteStorage):
            begun = 0

            async def begin_pending_decision(inner, **kwargs):
                audit = await super().begin_pending_decision(**kwargs)
                inner.begun += 1
                if inner.begun == 2:
                    gate.set()
                await gate.wait()
                return audit

        storage = RacingStorage()
        first = ScopedDecisionService(storage)
        second = ScopedDecisionService(storage)
        action_one, action_two = AsyncMock(), AsyncMock()
        results = await asyncio.wait_for(asyncio.gather(
            self.execute(flag="race-one", action=action_one, service=first),
            self.execute(flag="race-two", qq="10002", action=action_two, service=second),
        ), 3)
        self.assertEqual(sum(results), 1)
        self.assertEqual(action_one.await_count + action_two.await_count, 1)
        self.assertEqual(await storage.get_pending_decision_count(), 0)

    async def test_cancelled_replay_budget_survives_restart(self):
        audit = await self.pending()
        self.now += 11
        started = asyncio.Event()

        async def inspect(metadata):
            return evidence(metadata, "unhandled")

        async def replay(metadata):
            started.set()
            await asyncio.Event().wait()

        task = self.start(self.service.reconcile_pending(audit.id, inspector=inspect, replay=replay, notify=self.notify))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.service.state_store.load(audit.id))["retry_count"], 1)
        restored = ScopedDecisionService(self.storage, clock=lambda: self.now + 100)
        forbidden_replay = AsyncMock()
        self.assertEqual(await restored.reconcile_pending(
            audit.id, inspector=inspect, replay=forbidden_replay, notify=self.notify,
        ), "manual")
        forbidden_replay.assert_not_awaited()

    async def test_concurrent_duplicate_request_executes_one_external_action(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def action():
            started.set()
            await release.wait()

        external = AsyncMock(side_effect=action)
        first = self.start(self.execute(action=external))
        await asyncio.wait_for(started.wait(), 2)
        duplicate = self.start(self.execute(action=external))
        await asyncio.sleep(0)
        self.assertFalse(duplicate.done())
        release.set()
        self.assertEqual(await asyncio.gather(first, duplicate), [True, True])
        external.assert_awaited_once()
        self.assertEqual(Records.get_user_binding("10001").approved_count, 1)

    async def test_inspection_outage_budget_and_backoff_survive_restart(self):
        audit = await self.pending()
        inspector = AsyncMock(return_value=RequestEvidence(state="unavailable", detail="disconnected"))
        replay = AsyncMock()
        kwargs = dict(inspector=inspector, replay=replay, notify=self.notify)
        self.assertEqual(await self.service.reconcile_pending(audit.id, **kwargs), "backoff")
        first = await self.service.state_store.load(audit.id)
        self.assertEqual(first["inspection_attempts"], 1)
        self.assertEqual(first["next_inspection_at"], 1005.0)
        restored = ScopedDecisionService(self.storage, clock=lambda: self.now)
        self.assertEqual(await restored.reconcile_pending(audit.id, **kwargs), "backoff")
        self.assertEqual(inspector.await_count, 1)
        self.now = 1005.0
        self.assertEqual(await restored.reconcile_pending(audit.id, **kwargs), "backoff")
        self.assertEqual((await restored.state_store.load(audit.id))["next_inspection_at"], 1020.0)
        self.now = 1020.0
        self.assertEqual(await restored.reconcile_pending(audit.id, **kwargs), "backoff")
        self.assertEqual((await restored.state_store.load(audit.id))["next_inspection_at"], 1065.0)
        self.now = 1065.0
        self.assertEqual(await restored.reconcile_pending(audit.id, **kwargs), "manual")
        self.assertEqual(inspector.await_count, 4)
        self.assertEqual((await restored.state_store.load(audit.id))["inspection_attempts"], 4)
        self.assertEqual(await restored.reconcile_pending(audit.id, **kwargs), "manual")
        self.assertEqual(inspector.await_count, 4)
        replay.assert_not_awaited()
        self.assertFalse(restored.paused)

    async def test_reconnected_exact_inspector_resumes_after_read_only_backoff(self):
        audit = await self.pending()
        unavailable = AsyncMock(return_value=RequestEvidence(state="unavailable"))
        replay = AsyncMock()
        self.assertEqual(await self.service.reconcile_pending(
            audit.id, inspector=unavailable, replay=replay, notify=self.notify,
        ), "backoff")
        self.now += 5

        async def reconnected(metadata):
            return evidence(metadata, "applied")

        self.assertEqual(await self.service.reconcile_pending(
            audit.id, inspector=reconnected, replay=replay, notify=self.notify,
        ), "applied")
        metadata = await self.service.state_store.load(audit.id)
        self.assertEqual(metadata["inspection_attempts"], 0)
        self.assertEqual(metadata["next_inspection_at"], 0.0)
        self.assertEqual(await self.storage.get_pending_decision_count(), 0)
        replay.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
