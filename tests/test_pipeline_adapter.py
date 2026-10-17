"""Verify the async adapter consumes pure pipeline steps in observable order."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.gatekeeper.config import GroupConfig
from src.gatekeeper.models import RuleConditionStatus, StageRuntime
from src.plugins.group_request_manager import main as manager


def stage(index, actions, target=None):
    return StageRuntime(
        index=index,
        name=f"分组{index}",
        conditions=["known"],
        mode="all_pass",
        on_true=actions,
        on_false=["reject"],
        on_unknown=["ignore"],
        on_true_target=target,
    )


class PipelineAdapterTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = manager.JoinRequestContext(
            group_id="90001",
            user_id="10001",
            group_config=GroupConfig(group_id="90001"),
            bot=SimpleNamespace(),
        )
        context.condition_statuses = {"known": RuleConditionStatus("true")}
        return context

    async def test_each_trace_is_visible_before_its_effect_and_next_stage(self):
        context = self.context()
        observed = []

        async def notify(_bot, message, **_kwargs):
            observed.append(("notify", len(context.stage_results)))
            self.assertIn("next(分组2)", message)

        async def warn(_context, _stage, _outcome, _reasons, _result):
            observed.append(("warn", len(context.stage_results)))

        with patch.object(manager, "_notify_admins", new=notify), patch.dict(
            manager.ACTION_HANDLERS, {"log_warn": warn}
        ):
            execution = await manager._run_stage_pipeline(
                context,
                [stage(1, ["notify_admin", "next"], 2), stage(2, ["log_warn", "approve"])],
            )

        self.assertEqual(observed, [("notify", 1), ("warn", 2)])
        self.assertEqual(execution.dispatch_result.flow_action, "approve")
        self.assertEqual(execution.stage.index, 2)

    async def test_effect_failure_keeps_current_trace_and_stops_later_effects(self):
        context = self.context()
        warn = AsyncMock()
        with patch.object(
            manager, "_notify_admins", new=AsyncMock(side_effect=RuntimeError("offline"))
        ), patch.dict(manager.ACTION_HANDLERS, {"log_warn": warn}):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await manager._run_stage_pipeline(
                    context,
                    [stage(1, ["notify_admin", "next"], 2), stage(2, ["log_warn", "approve"])],
                )

        self.assertEqual(len(context.stage_results), 1)
        self.assertTrue(context.stage_results[0].startswith("分组1 |"))
        warn.assert_not_awaited()

    async def test_terminal_next_adds_fallback_trace_without_approval_handler(self):
        context = self.context()
        approve = AsyncMock()
        with patch.dict(manager.ACTION_HANDLERS, {"approve": approve}):
            execution = await manager._run_stage_pipeline(context, [stage(1, ["next"])])

        self.assertEqual(execution.dispatch_result.flow_action, "approve")
        self.assertEqual(len(context.stage_results), 2)
        self.assertIn("全部审核分组均已放行", context.stage_results[-1])
        approve.assert_not_awaited()

    async def test_action_dispatch_can_select_a_different_next_stage(self):
        context = self.context()

        async def redirect(_context, _stage, _outcome, _reasons, result):
            result.target_stage_index = 3

        with patch.dict(manager.ACTION_HANDLERS, {"notify_admin": redirect}):
            execution = await manager._run_stage_pipeline(
                context,
                [
                    stage(1, ["notify_admin", "next"], 2),
                    stage(2, ["reject"]),
                    stage(3, ["approve"]),
                ],
            )

        self.assertEqual(execution.stage.index, 3)
        self.assertEqual(execution.dispatch_result.flow_action, "approve")
        self.assertEqual(len(context.stage_results), 2)
        self.assertTrue(context.stage_results[-1].startswith("分组3 |"))
