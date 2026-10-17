"""审核核心的规则、路由和轨迹契约，无需初始化 NoneBot。"""

import unittest
from types import SimpleNamespace

from src.gatekeeper.conditions import build_rule_condition_statuses
from src.gatekeeper.config import GroupConfig, ReviewStageConfig
from src.gatekeeper.models import (
    CheckResult,
    RuleConditionStatus,
    StageDispatchResult,
    StageRuntime,
)
from src.gatekeeper.pipeline import get_review_stages, iter_pipeline


def make_stage(index=1, *, conditions=None, mode="all_pass", **branches):
    values = {
        "index": index,
        "name": f"筛选组{index}",
        "conditions": conditions if conditions is not None else ["follow"],
        "mode": mode,
        "on_true": ["approve"],
        "on_false": ["reject"],
        "on_unknown": ["ignore"],
    }
    values.update(branches)
    return StageRuntime(**values)


def finish_pipeline(stages, statuses):
    steps = []
    pipeline = iter_pipeline(stages, statuses)
    while True:
        try:
            steps.append(next(pipeline))
        except StopIteration as finished:
            return steps, finished.value


class CoreConditionTests(unittest.TestCase):
    def build_statuses(self, config, conditions, **overrides):
        inputs = {
            "qq_level": None,
            "bili_search_result": CheckResult("not_required"),
            "bili_level": None,
            "follow_result": CheckResult("not_required"),
            "medal_result": CheckResult("not_required"),
            "leave_record": None,
            "conflict_qq": None,
            "owned_other_bili_uids": [],
        }
        inputs.update(overrides)
        return build_rule_condition_statuses(config, set(conditions), **inputs)

    def test_search_failure_and_unavailable_level_keep_distinct_states(self):
        statuses = self.build_statuses(
            GroupConfig(group_id="1", required_bili_level=2),
            {"bili_account", "bili_level"},
            bili_search_result=CheckResult("failed", "未搜索到用户"),
        )

        self.assertEqual(statuses["bili_account"].state, "false")
        self.assertEqual(statuses["bili_level"].state, "unknown")
        self.assertEqual(statuses["bili_level"].reason, "未搜索到B站账号，无法读取 B站等级")

    def test_follow_unknown_retains_api_diagnostic(self):
        detail = "共同关注不可见或无法读取；关注列表仅能读取 100/156 项"
        statuses = self.build_statuses(
            GroupConfig(group_id="1", target_uids=[123]),
            {"follow"},
            follow_result=CheckResult("inaccessible", detail),
        )

        self.assertEqual(statuses["follow"], RuleConditionStatus("unknown", f"{detail}，按配置忽略"))

    def test_missing_thresholds_and_targets_are_unknown_despite_passed_observations(self):
        statuses = self.build_statuses(
            GroupConfig(group_id="1"),
            {"qq_level", "bili_level", "follow", "medal"},
            qq_level=80,
            bili_search_result=CheckResult("passed", matched_target_uid=123),
            bili_level=6,
            follow_result=CheckResult("passed"),
            medal_result=CheckResult("passed"),
        )

        self.assertEqual(
            {name: status.state for name, status in statuses.items()},
            {
                "qq_level": "unknown",
                "bili_level": "unknown",
                "follow": "unknown",
                "medal": "unknown",
            },
        )
        for status in statuses.values():
            self.assertIn("未配置", status.reason)

    def test_explicit_negative_observations_use_configured_reasons(self):
        statuses = self.build_statuses(
            GroupConfig(
                group_id="1",
                required_qq_level=10,
                target_uids=[123],
                reject_reasons={"qq_level": "QQ等级未达标", "follow": "请先关注主播"},
            ),
            {"qq_level", "follow"},
            qq_level=4,
            follow_result=CheckResult("failed"),
        )

        self.assertEqual(statuses["qq_level"], RuleConditionStatus("false", "QQ等级未达标"))
        self.assertEqual(statuses["follow"], RuleConditionStatus("false", "请先关注主播"))

    def test_absent_qq_does_not_imply_no_leave_history(self):
        statuses = self.build_statuses(GroupConfig(group_id="1"), {"no_leave_record"}, leave_record_known=False)

        self.assertEqual(statuses["no_leave_record"].state, "unknown")
        self.assertIn("未提供 QQ", statuses["no_leave_record"].reason)

    def test_leave_record_only_needs_its_timestamp(self):
        statuses = self.build_statuses(
            GroupConfig(group_id="1"),
            {"no_leave_record"},
            leave_record=SimpleNamespace(leave_time="2026-10-17T08:09:10"),
        )

        self.assertEqual(
            statuses["no_leave_record"],
            RuleConditionStatus("false", "您曾于 2026-10-17 08:09:10 退群"),
        )

    def test_empty_identity_lookup_is_unknown_until_lookup_is_confirmed(self):
        config = GroupConfig(group_id="1")
        conditions = {"no_conflict", "no_identity_change"}
        identity = CheckResult("passed", matched_target_uid=123)
        unknown = self.build_statuses(config, conditions, bili_search_result=identity)
        confirmed = self.build_statuses(
            config,
            conditions,
            bili_search_result=identity,
            conflict_lookup_known=True,
            identity_change_lookup_known=True,
        )

        self.assertEqual({value.state for value in unknown.values()}, {"unknown"})
        self.assertEqual({value.state for value in confirmed.values()}, {"true"})

    def test_missing_identity_remains_unknown_even_when_lookups_are_confirmed(self):
        for identity in (CheckResult("inaccessible"), CheckResult("passed", matched_target_uid=None)):
            with self.subTest(identity=identity):
                statuses = self.build_statuses(
                    GroupConfig(group_id="1"),
                    {"no_conflict", "no_identity_change"},
                    bili_search_result=identity,
                    conflict_lookup_known=True,
                    identity_change_lookup_known=True,
                )
                self.assertEqual({value.state for value in statuses.values()}, {"unknown"})


class CorePipelineTests(unittest.TestCase):
    def test_false_has_priority_and_only_false_reasons_explain_rejection(self):
        stage = make_stage(conditions=["follow", "qq_level", "medal"])
        steps, result = finish_pipeline(
            [stage],
            {
                "follow": RuleConditionStatus("unknown", "关注接口不可用"),
                "qq_level": RuleConditionStatus("false", "QQ等级不足"),
                "medal": RuleConditionStatus("false", "未持有粉丝牌"),
            },
        )

        self.assertEqual(result.outcome, "false")
        self.assertEqual(result.dispatch_result.flow_action, "reject")
        self.assertEqual(result.reasons, ["QQ等级不足", "未持有粉丝牌"])
        self.assertEqual(
            steps[0].trace, ("筛选组1 | all_pass[follow,qq_level,medal] | 未通过 | reject | QQ等级不足；未持有粉丝牌")
        )

    def test_any_pass_accepts_true_despite_unknown_and_excludes_unknown_reason(self):
        _, result = finish_pipeline(
            [make_stage(mode="any_pass", conditions=["follow", "medal"])],
            {
                "follow": RuleConditionStatus("unknown", "关注接口不可用"),
                "medal": RuleConditionStatus("true"),
            },
        )

        self.assertEqual(result.outcome, "true")
        self.assertEqual(result.reasons, ["通过阶段：筛选组1"])

    def test_unknown_branch_preserves_only_unknown_reasons(self):
        _, result = finish_pipeline(
            [make_stage(conditions=["qq_level", "follow"])],
            {
                "qq_level": RuleConditionStatus("true"),
                "follow": RuleConditionStatus("unknown", "关注列表不完整"),
            },
        )

        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.reasons, ["关注列表不完整"])

    def test_branch_reason_overrides_observation_reasons(self):
        for outcome in ("true", "false", "unknown"):
            with self.subTest(outcome=outcome):
                stage = make_stage(**{f"on_{outcome}_reason": "管理员配置的分组原因"})
                _, result = finish_pipeline([stage], {"follow": RuleConditionStatus(outcome, "原始原因")})
                self.assertEqual(result.reasons, ["管理员配置的分组原因"])

    def test_empty_failure_reasons_have_stable_fallbacks(self):
        for outcome, expected in (
            ("false", "未满足阶段规则"),
            ("unknown", "阶段所需条件暂时无法完整判断"),
        ):
            with self.subTest(outcome=outcome):
                _, result = finish_pipeline([make_stage()], {"follow": RuleConditionStatus(outcome)})
                self.assertEqual(result.reasons, [expected])

    def test_explicit_routing_skips_unvisited_stages_and_records_planned_effects(self):
        first = make_stage(on_unknown=["next", "notify_admin"], on_unknown_target=30)
        unvisited = make_stage(20, conditions=["unread_observation"])
        last = make_stage(30, conditions=["medal"])
        steps, result = finish_pipeline(
            [last, unvisited, first],
            {
                "follow": RuleConditionStatus("unknown", "关注列表不可见"),
                "medal": RuleConditionStatus("true"),
            },
        )

        self.assertEqual([step.execution.stage.index for step in steps], [1, 30])
        self.assertEqual(result.dispatch_result.flow_action, "approve")
        self.assertEqual(steps[0].execution.actions, ["next", "notify_admin"])
        self.assertTrue(steps[0].dispatch_actions)
        self.assertEqual(
            steps[0].trace, ("筛选组1 | all_pass[follow] | 无法判断 | next(分组30),notify_admin | 关注列表不可见")
        )

    def test_sequential_routing_uses_sorted_indexes(self):
        steps, result = finish_pipeline(
            [make_stage(10), make_stage(2, on_true=["next"])],
            {"follow": RuleConditionStatus("true")},
        )

        self.assertEqual([step.execution.stage.index for step in steps], [2, 10])
        self.assertEqual(result.stage.index, 10)

    def test_executor_can_override_planned_flow_and_target(self):
        pipeline = iter_pipeline(
            [make_stage(), make_stage(2, on_true=["ignore"])],
            {"follow": RuleConditionStatus("true")},
        )
        first = next(pipeline)
        self.assertEqual(first.execution.dispatch_result.flow_action, "approve")
        second = pipeline.send(StageDispatchResult("next", target_stage_index=2))
        self.assertEqual(second.execution.stage.index, 2)

        with self.assertRaises(StopIteration) as finished:
            next(pipeline)
        self.assertEqual(finished.exception.value.dispatch_result.flow_action, "ignore")

    def test_executor_can_stop_before_an_unreadable_next_stage(self):
        pipeline = iter_pipeline(
            [make_stage(on_true=["next"]), make_stage(2, conditions=["unread_observation"])],
            {"follow": RuleConditionStatus("true")},
        )
        next(pipeline)

        with self.assertRaises(StopIteration) as finished:
            pipeline.send(StageDispatchResult("ignore"))
        self.assertEqual(finished.exception.value.dispatch_result.flow_action, "ignore")

    def test_terminal_true_next_emits_synthetic_approval_without_dispatching_it(self):
        steps, result = finish_pipeline(
            [make_stage(on_true=["next", "notify_admin"])],
            {"follow": RuleConditionStatus("true")},
        )

        self.assertEqual(len(steps), 2)
        self.assertTrue(steps[0].dispatch_actions)
        self.assertFalse(steps[1].dispatch_actions)
        self.assertEqual(steps[1].execution.actions, ["approve"])
        self.assertEqual(steps[1].trace, ("筛选组1 | all_pass[follow] | 通过 | approve | 全部审核分组均已放行"))
        self.assertIs(result, steps[1].execution)
        self.assertEqual(result.dispatch_result.flow_action, "approve")

    def test_terminal_false_or_unknown_next_fails_after_emitting_the_stage(self):
        for outcome in ("false", "unknown"):
            with self.subTest(outcome=outcome):
                pipeline = iter_pipeline(
                    [make_stage(**{f"on_{outcome}": ["next"]})],
                    {"follow": RuleConditionStatus(outcome)},
                )
                step = next(pipeline)
                self.assertEqual(step.execution.outcome, outcome)
                with self.assertRaisesRegex(ValueError, "不存在下一个分组"):
                    next(pipeline)

    def test_missing_observation_raises_only_when_its_stage_is_reached(self):
        pipeline = iter_pipeline(
            [make_stage(on_true=["next"]), make_stage(2, conditions=["medal"])],
            {"follow": RuleConditionStatus("true")},
        )
        self.assertEqual(next(pipeline).execution.stage.index, 1)

        with self.assertRaisesRegex(KeyError, "medal"):
            next(pipeline)

    def test_missing_target_and_cycle_fail_after_the_preceding_stage(self):
        cases = (
            ([make_stage(on_true=["next"], on_true_target=99)], "不存在的分组 99", [1]),
            (
                [make_stage(on_true=["next"]), make_stage(2, on_true=["next"], on_true_target=1)],
                "形成循环",
                [1, 2],
            ),
        )
        for stages, message, expected_indexes in cases:
            with self.subTest(message=message):
                pipeline = iter_pipeline(stages, {"follow": RuleConditionStatus("true")})
                self.assertEqual([next(pipeline).execution.stage.index for _ in expected_indexes], expected_indexes)
                with self.assertRaisesRegex(ValueError, message):
                    next(pipeline)

    def test_empty_pipeline_raises_on_first_advance(self):
        pipeline = iter_pipeline([], {})
        with self.assertRaisesRegex(ValueError, "审核分组不能为空"):
            next(pipeline)

    def test_missing_flow_action_preserves_trace_before_dispatch_error(self):
        pipeline = iter_pipeline(
            [make_stage(on_true=["notify_admin"])],
            {"follow": RuleConditionStatus("true")},
        )

        step = next(pipeline)
        self.assertIn("notify_admin", step.trace)
        with self.assertRaisesRegex(ValueError, "阶段动作缺少流转动作"):
            next(pipeline)


class CorePipelineConfigurationTests(unittest.TestCase):
    def test_compilation_orders_stages_and_supplies_default_names(self):
        config = GroupConfig(
            group_id="1",
            review_stages=[
                ReviewStageConfig(index=9, conditions=["medal"], on_true=["approve"]),
                ReviewStageConfig(index=3, name="前置条件", conditions=["follow"]),
            ],
        )

        stages = get_review_stages(config)

        self.assertEqual([stage.index for stage in stages], [3, 9])
        self.assertEqual([stage.name for stage in stages], ["前置条件", "阶段9"])

    def test_configuration_rejects_missing_pipeline(self):
        with self.assertRaisesRegex(ValueError, "未配置审核分组管道"):
            get_review_stages(GroupConfig(group_id="1"))

    def test_compilation_rejects_duplicate_self_and_missing_targets(self):
        cases = (
            (
                [ReviewStageConfig(index=1, conditions=["follow"]), ReviewStageConfig(index=1, conditions=["medal"])],
                "编号不能重复",
            ),
            ([ReviewStageConfig(index=1, conditions=["follow"], on_true_target=1)], "不能指向自己"),
            ([ReviewStageConfig(index=1, conditions=["follow"], on_true_target=99)], "不存在的分组 99"),
        )
        for stages, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                get_review_stages(GroupConfig(group_id="1", review_stages=stages))


if __name__ == "__main__":
    unittest.main()
