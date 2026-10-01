"""Regression coverage for overlapping requests and ambiguous approval results."""

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11.exception import NetworkError
from nonebot.exception import FinishedException
from src.plugins.group_request_manager import database as database_module
from src.plugins.group_request_manager import config as config_module
from src.plugins.group_request_manager import main as manager
from src.plugins.group_request_manager.bili_api import CheckResult
from src.plugins.group_request_manager.config import ConfigManager, GroupConfig, PluginConfig, ReviewStageConfig
from src.plugins.group_request_manager.decision_runtime import DecisionLocks
from src.plugins.group_request_manager.request_record import JoinRequestRecordManager as Records


def request_event(flag, *, qq=10001, name="first-name"):
    return SimpleNamespace(flag=flag, sub_type="add", group_id=90001, user_id=qq, comment=name)


def audit_kwargs(*, qq="10001", uid=888):
    return dict(group_id="90001", qq=qq, bili_name="test-name", bili_uid=uid, result="approved", reasons=["通过"])


class DecisionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for attribute, name in (
            ("DATABASE_FILE", "gatekeeper.sqlite3"),
            ("LEGACY_JOIN_REQUEST_FILE", "absent.json"),
            ("LEGACY_LEAVE_RECORDS_PATH", "absent-leaves"),
            ("VERY_LEGACY_LEAVE_RECORDS_PATH", "absent-old-leaves"),
        ):
            self.stack.enter_context(patch.object(database_module, attribute, root / name))
        database_module.reset_database_state()
        self.addCleanup(database_module.reset_database_state)
        self.stack.enter_context(patch.object(manager, "_automatic_decisions_paused", False))
        self.stack.enter_context(patch.object(manager, "_decision_locks", DecisionLocks()))
        self.stack.enter_context(patch.object(manager, "_notify_admins", new=AsyncMock()))

        async def immediate_storage(function, *args, **kwargs):
            # Keep scheduling checkpoints deterministic while exercising real SQLite writes.
            return function(*args, **kwargs)

        self.stack.enter_context(patch.object(manager, "run_storage", new=immediate_storage))
        self.tasks = []
        self.addAsyncCleanup(self.cancel_remaining_tasks)

    async def cancel_remaining_tasks(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    def configure_identity_checks(self, condition):
        group = GroupConfig(group_id="90001", review_stages=[ReviewStageConfig(
            index=1, conditions=["bili_account", condition],
            on_true=["approve"], on_false=["reject"], on_unknown=["ignore"],
        )])
        self.stack.enter_context(patch.object(ConfigManager, "get_group_config", return_value=group))
        self.stack.enter_context(patch.object(ConfigManager, "get_bili_cookie", return_value="test-cookie"))
        second_search = asyncio.Event()
        searches = 0

        async def search(name):
            nonlocal searches
            searches += 1
            if searches == 2:
                second_search.set()
            return CheckResult("passed", matched_target_uid=889 if name == "second-name" else 888, matched_level=6)

        async def run_api(cookie, operation):
            return await operation(SimpleNamespace(search_user_profile=search))

        self.stack.enter_context(patch.object(manager._bili_requests, "run", new=run_api))
        return second_search

    def blocking_bot(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def approve(**kwargs):
            if kwargs["flag"] == "first":
                started.set()
                await release.wait()

        return SimpleNamespace(self_id="90000", set_group_add_request=AsyncMock(side_effect=approve)), started, release

    async def check_identity_race(self, condition, second_event):
        second_search = self.configure_identity_checks(condition)
        bot, started, release = self.blocking_bot()
        first = self.start(manager.handle_group_request(bot, request_event("first")))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = self.start(manager.handle_group_request(bot, second_event))
        await asyncio.wait_for(second_search.wait(), timeout=2)
        self.assertEqual(bot.set_group_add_request.await_count, 1)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        self.assertEqual([call.kwargs["approve"] for call in bot.set_group_add_request.await_args_list], [True, False])
        audits = Records.get_all_user_audits(str(second_event.user_id))
        rejected_audits = [audit for audit in audits if audit.result == "rejected"]
        self.assertEqual(len(rejected_audits), 1)
        self.assertEqual(rejected_audits[0].condition_states[condition], "false")
        self.assertEqual(Records.get_owned_bili_uids("10001"), [888])
        self.assertEqual(Records.get_bili_binding(888).owner_qq, "10001")

    async def test_same_uid_cannot_be_approved_for_two_qqs(self):
        await self.check_identity_race("no_conflict", request_event("second", qq=10002))

    async def test_same_qq_cannot_bind_two_uids_concurrently(self):
        await self.check_identity_race("no_identity_change", request_event("second", name="second-name"))

    async def test_unrelated_identities_can_finish_while_first_approval_waits(self):
        self.configure_identity_checks("no_conflict")
        bot, started, release = self.blocking_bot()
        first = self.start(manager.handle_group_request(bot, request_event("first")))
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(manager.handle_group_request(bot, request_event("second", qq=10002, name="second-name")), timeout=2)
        self.assertFalse(first.done())
        self.assertEqual(Records.get_bili_binding(889).owner_qq, "10002")
        release.set()
        await first

    async def test_identity_lookup_failure_remains_unknown(self):
        self.configure_identity_checks("no_conflict")
        bot = SimpleNamespace(self_id="90000", set_group_add_request=AsyncMock())
        with patch.object(Records, "get_conflict_qq", side_effect=RuntimeError("database offline")):
            await manager.handle_group_request(bot, request_event("lookup-failed"))
        audit = Records.get_all_user_audits("10001")[0]
        self.assertEqual(audit.condition_states["no_conflict"], "unknown")
        self.assertEqual(audit.result, "ignored")
        bot.set_group_add_request.assert_not_awaited()

    async def test_in_flight_duplicate_waits_without_pausing_later_requests(self):
        bot, started, release = self.blocking_bot()
        first = self.start(manager._set_group_request_with_durable_audit(
            bot, request_event("first"), approve=True, audit_kwargs=audit_kwargs(),
        ))
        await asyncio.wait_for(started.wait(), timeout=2)
        duplicate = self.start(manager._set_group_request_with_durable_audit(
            bot, request_event("first"), approve=True, audit_kwargs=audit_kwargs(),
        ))
        await asyncio.sleep(0)
        self.assertFalse(manager._automatic_decisions_paused)
        self.assertFalse(duplicate.done())
        release.set()
        self.assertEqual(await asyncio.gather(first, duplicate), [True, True])
        self.assertEqual(Records.get_pending_decision_count(), 0)
        self.assertTrue(await manager._set_group_request_with_durable_audit(
            bot, request_event("next", qq=10002), approve=True, audit_kwargs=audit_kwargs(qq="10002", uid=889),
        ))
        self.assertFalse(manager._automatic_decisions_paused)
        self.assertEqual(bot.set_group_add_request.await_count, 2)

    async def test_orphan_pending_duplicate_still_requires_confirmation(self):
        audit = Records.begin_pending_decision(**audit_kwargs(), request_key="90000:90001:add:orphan")
        bot = SimpleNamespace(self_id="90000", set_group_add_request=AsyncMock())
        self.assertFalse(await manager._set_group_request_with_durable_audit(
            bot, request_event("orphan"), approve=True, audit_kwargs=audit_kwargs(),
        ))
        self.assertTrue(manager._automatic_decisions_paused)
        self.assertEqual(Records.get_pending_decisions()[0].id, audit.id)
        bot.set_group_add_request.assert_not_awaited()

    async def test_lost_response_stays_pending_and_manual_confirmation_restores_binding(self):
        accepted = []

        async def accepted_then_timeout(**kwargs):
            accepted.append(kwargs["flag"])
            raise NetworkError("WebSocket call api timeout")

        bot = SimpleNamespace(self_id="90000", set_group_add_request=AsyncMock(side_effect=accepted_then_timeout))
        self.assertFalse(await manager._set_group_request_with_durable_audit(
            bot, request_event("lost-response"), approve=True, audit_kwargs=audit_kwargs(),
        ))
        audit = Records.get_pending_decisions()[0]
        self.assertEqual(accepted, ["lost-response"])
        self.assertEqual(audit.result, "approved")
        self.assertEqual(audit.action_status, "pending")
        self.assertTrue(manager._automatic_decisions_paused)
        self.assertIsNone(Records.get_bili_binding(888))
        with patch.object(manager, "_is_private_superadmin_event", return_value=True), patch.object(
            manager.resolve_pending_decision, "finish", new=AsyncMock(side_effect=FinishedException),
        ):
            with self.assertRaises(FinishedException):
                await manager.handle_resolve_pending_decision(SimpleNamespace(), f"{audit.id} applied")
        self.assertEqual(Records.get_pending_decision_count(), 0)
        self.assertFalse(manager._automatic_decisions_paused)
        self.assertEqual(Records.get_bili_binding(888).owner_qq, "10001")
        self.assertEqual(Records.get_all_user_audits("10001")[0].action_status, "applied")

    async def test_cancelled_approval_stays_pending(self):
        bot, started, _ = self.blocking_bot()
        task = self.start(manager._set_group_request_with_durable_audit(
            bot, request_event("first"), approve=True, audit_kwargs=audit_kwargs(),
        ))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(manager._automatic_decisions_paused)
        self.assertEqual(Records.get_pending_decision_count(), 1)
        self.assertIsNone(Records.get_bili_binding(888))

    async def test_clear_binding_waits_for_approval_before_removing_identity(self):
        self.configure_identity_checks("no_conflict")
        bot, started, release = self.blocking_bot()
        first = self.start(manager.handle_group_request(bot, request_event("first")))
        await asyncio.wait_for(started.wait(), timeout=2)
        with patch.object(manager, "_is_private_admin_event", return_value=True), patch.object(
            manager.clear_identity_binding, "finish", new=AsyncMock(side_effect=FinishedException),
        ):
            clearing = self.start(manager.handle_clear_identity_binding(SimpleNamespace(), "10001"))
            await asyncio.sleep(0)
            self.assertFalse(clearing.done())
            release.set()
            await first
            with self.assertRaises(FinishedException):
                await clearing
        self.assertIsNone(Records.get_bili_binding(888))


class ConfigurationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.overrides_file = root / "group_overrides.json"
        self.config = PluginConfig(groups={"90001": GroupConfig(group_id="90001", enabled=True, required_qq_level=10)})
        self.original = self.config.groups["90001"]
        self.stack.enter_context(patch.object(config_module, "GROUP_OVERRIDES_FILE", self.overrides_file))
        self.stack.enter_context(patch.object(ConfigManager, "_config", self.config))
        self.stack.enter_context(patch.object(manager, "_is_private_superadmin_event", return_value=True))
        self.finish = self.stack.enter_context(patch.object(manager.set_group_config, "finish", new=AsyncMock(side_effect=FinishedException)))

    async def command(self, args):
        with self.assertRaises(FinishedException):
            await manager.handle_set_group_config(SimpleNamespace(), args)

    async def test_invalid_later_parameter_does_not_mutate_running_config(self):
        for args in ("90001 enable off required_qq_level invalid", "90001 enable off unsupported value"):
            with self.subTest(args=args):
                await self.command(args)
                self.assertTrue(self.original.enabled)
                self.assertIs(self.config.groups["90001"], self.original)
                self.assertFalse(self.overrides_file.exists())

    async def test_save_failure_keeps_original_config(self):
        with patch.object(ConfigManager, "_save_group_override", side_effect=OSError("disk full")):
            await self.command("90001 enable off")
        self.assertTrue(self.original.enabled)
        self.assertIs(self.config.groups["90001"], self.original)
        self.assertIn("配置保存失败", self.finish.await_args.args[0])

    async def test_valid_command_persists_before_publishing_new_config(self):
        await self.command("90001 enable off required_qq_level 20")
        self.assertTrue(self.original.enabled)
        self.assertFalse(self.config.groups["90001"].enabled)
        self.assertEqual(self.config.groups["90001"].required_qq_level, 20)
        saved = json.loads(self.overrides_file.read_text(encoding="utf-8"))["groups"]["90001"]
        self.assertEqual(saved, {"enabled": False, "required_qq_level": 20})


class DecisionLockCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_does_not_prevent_later_identity_decisions(self):
        locks = DecisionLocks()
        completed = asyncio.Event()

        async def wait_for_identity():
            async with locks.hold("qq:1", "uid:2"):
                completed.set()

        async with locks.hold("uid:2"):
            waiter = asyncio.create_task(wait_for_identity())
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        await asyncio.wait_for(wait_for_identity(), timeout=2)
        self.assertTrue(completed.is_set())
