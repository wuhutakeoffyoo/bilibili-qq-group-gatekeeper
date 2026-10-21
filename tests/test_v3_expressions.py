"""Safe v3 expressions, raw measurements and unchanged v2 trace contracts."""

import itertools
import unittest

from pydantic import ValidationError

from src.gatekeeper.conditions import build_rule_condition_statuses
from src.gatekeeper.config import GroupConfig, ReviewStageConfig
from src.gatekeeper.expressions import compile_expression, evaluate_expression
from src.gatekeeper.formatters import build_stage_reject_reasons, format_stage_results
from src.gatekeeper.models import CheckResult, RuleConditionStatus
from src.gatekeeper.pipeline import collect_pipeline_conditions, get_review_stages, iter_pipeline


def execute(stage, statuses):
    pipeline = iter_pipeline([stage], statuses)
    traces = []
    while True:
        try:
            traces.append(next(pipeline).trace)
        except StopIteration as result:
            return traces, result.value


class ExpressionTruthTests(unittest.TestCase):
    def test_complete_binary_kleene_truth_tables_and_operator_aliases(self):
        states = ("true", "false", "unknown")
        and_table = ("true", "false", "unknown", "false", "false", "false", "unknown", "false", "unknown")
        or_table = ("true", "true", "true", "true", "false", "unknown", "true", "unknown", "unknown")
        for index, (left, right) in enumerate(itertools.product(states, repeat=2)):
            statuses = {"follow": RuleConditionStatus(left), "medal": RuleConditionStatus(right)}
            for operator in ("and", "&&"):
                with self.subTest(left=left, right=right, operator=operator):
                    self.assertEqual(evaluate_expression(f"follow {operator} medal", statuses), and_table[index])
            for operator in ("or", "||"):
                with self.subTest(left=left, right=right, operator=operator):
                    self.assertEqual(evaluate_expression(f"follow {operator} medal", statuses), or_table[index])

    def test_complete_negation_table_and_nested_unknown(self):
        for state, expected in (("true", "false"), ("false", "true"), ("unknown", "unknown")):
            for expression in ("not follow", "!follow", "!(!(!follow))"):
                with self.subTest(state=state, expression=expression):
                    self.assertEqual(evaluate_expression(expression, {"follow": RuleConditionStatus(state)}), expected)
        self.assertEqual(evaluate_expression("not (follow or medal)", {}), "unknown")

    def test_numeric_comparisons_use_observation_instead_of_threshold_boolean(self):
        statuses = {"qq_level": RuleConditionStatus("false", "未达到旧门槛", observation=20)}
        self.assertEqual(evaluate_expression("qq_level", statuses), "false")
        self.assertEqual(evaluate_expression("qq_level >= 16", statuses), "true")
        self.assertEqual(evaluate_expression("10 <= qq_level < 25", statuses), "true")
        self.assertEqual(evaluate_expression("qq_level != 20", statuses), "false")
        self.assertEqual(evaluate_expression("qq_level == 20", statuses), "true")

    def test_comparisons_without_measurements_and_nonfinite_are_unknown(self):
        for raw in (None, True, float("inf"), float("nan"), 10**1000):
            with self.subTest(raw=repr(raw)[:25]):
                status = RuleConditionStatus("true", observation=raw)
                self.assertEqual(evaluate_expression("qq_level >= 1", {"qq_level": status}), "unknown")
        self.assertEqual(evaluate_expression("qq_level >= 1", {}), "unknown")

    def test_blueprint_medal_numeric_name_and_explicit_alias_are_equivalent(self):
        statuses = {"follow": RuleConditionStatus("false"), "medal": RuleConditionStatus("false", observation=2)}
        self.assertEqual(evaluate_expression("medal", statuses), "false")
        self.assertEqual(evaluate_expression("follow || medal >= 1", statuses), "true")
        self.assertEqual(evaluate_expression("follow || medal_level >= 1", statuses), "true")
        self.assertEqual(compile_expression("follow || medal >= 1").conditions, ("follow", "medal"))

    def test_dependencies_are_exact_and_medal_level_fetches_medal(self):
        expression = compile_expression("qq_level >= 16 && (follow || medal_level >= 10) && !no_conflict")
        self.assertEqual(expression.conditions, ("qq_level", "follow", "medal", "no_conflict"))
        stage = ReviewStageConfig(index=1, when=expression.source, conditions=["bili_account"])
        self.assertEqual(stage.conditions, list(expression.conditions))
        runtime = get_review_stages(GroupConfig(group_id="1", review_stages=[stage]))
        self.assertEqual(collect_pipeline_conditions(runtime), set(expression.conditions))

    def test_raw_measurements_survive_missing_v2_threshold(self):
        statuses = build_rule_condition_statuses(
            GroupConfig(group_id="1"), {"qq_level", "bili_level", "medal"},
            qq_level=16, bili_search_result=CheckResult("passed"), bili_level=6,
            follow_result=CheckResult("not_required"), medal_result=CheckResult("failed", matched_level=9),
            leave_record=None, conflict_qq=None, owned_other_bili_uids=[],
        )
        self.assertEqual({status.state for status in statuses.values()}, {"unknown"})
        self.assertEqual(evaluate_expression("qq_level >= 16 and bili_level == 6 and medal_level >= 9", statuses), "true")

    def test_unavailable_medal_observation_remains_unknown(self):
        for result in (CheckResult("inaccessible", matched_level=10), CheckResult("failed")):
            statuses = build_rule_condition_statuses(
                GroupConfig(group_id="1", target_medal_uids=[9]), {"medal"},
                qq_level=None, bili_search_result=CheckResult("passed"), bili_level=None,
                follow_result=CheckResult("not_required"), medal_result=result,
                leave_record=None, conflict_qq=None, owned_other_bili_uids=[],
            )
            self.assertEqual(evaluate_expression("medal_level >= 1", statuses), "unknown")


class ExpressionSafetyTests(unittest.TestCase):
    def test_rejects_calls_attributes_subscripts_and_non_whitelisted_operators(self):
        for source in (
            "__import__('os').system('echo bad')", "follow.real", "follow[0]", "[follow]", "{'x': follow}",
            "(x for x in [])", "lambda: follow", "qq_level + 1 > 2", "qq_level ** 2 > 1", "follow & medal",
            "follow is True", "follow in []", "qq_level > True", "qq_level > '1'", "unknown_name",
            "(x := follow)", "f'{follow}'", "medal_level", "notfollow", "follow == 1", "qq_level > 1e999",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                compile_expression(source)

    def test_rejects_excess_length_depth_and_node_count(self):
        for source in ("not " * 30 + "follow", " and ".join(["follow"] * 130), " " * 2049, "qq_level > " + "9" * 500):
            with self.subTest(source=source[:40]), self.assertRaises(ValueError):
                compile_expression(source)

    def test_model_validation_rejects_expression_before_runtime(self):
        with self.assertRaises(ValidationError):
            ReviewStageConfig(index=1, when="open('secret')")
        with self.assertRaises(ValidationError):
            ReviewStageConfig(index=1)


class ExpressionFeedbackTests(unittest.TestCase):
    def test_negation_rejection_does_not_claim_satisfied_follow_failed(self):
        stage = get_review_stages(GroupConfig(group_id="1", review_stages=[ReviewStageConfig(
            index=1, name="反向规则", when="not follow", on_true=["approve"], on_false=["reject"],
        )]))[0]
        statuses = {"follow": RuleConditionStatus("true")}
        traces, result = execute(stage, statuses)
        feedback = "；".join(build_stage_reject_reasons(stage, result.outcome, statuses, result.reasons))
        self.assertIn("not follow", feedback)
        self.assertIn("关注条件已满足", feedback)
        self.assertNotIn("未检测到", feedback)
        self.assertNotIn("关注条件未满足", feedback)
        self.assertIn("表达式", "\n".join(format_stage_results(traces)))

    def test_expression_false_selects_relevant_causes_and_keeps_override(self):
        stage = get_review_stages(GroupConfig(group_id="1", review_stages=[ReviewStageConfig(
            index=1, when="qq_level >= 16 and (follow or medal_level >= 10)", on_false_reason="管理员说明",
        )]))[0]
        statuses = {
            "qq_level": RuleConditionStatus("true", observation=10),
            "follow": RuleConditionStatus("true"),
            "medal": RuleConditionStatus("false", "粉丝牌不存在", observation=0),
        }
        _, result = execute(stage, statuses)
        self.assertEqual(result.outcome, "false")
        self.assertEqual(result.reasons[0], "管理员说明")
        self.assertIn("QQ等级当前10", result.reasons[1])
        self.assertNotIn("粉丝牌不存在", result.reasons[1])

    def test_unknown_expression_rejection_does_not_invent_negative_observation(self):
        stage = get_review_stages(GroupConfig(group_id="1", review_stages=[ReviewStageConfig(
            index=1, when="bili_level >= 5", on_unknown=["reject"],
        )]))[0]
        statuses = {"bili_level": RuleConditionStatus("unknown", "接口不可用")}
        _, result = execute(stage, statuses)
        text = "；".join(build_stage_reject_reasons(stage, result.outcome, statuses, result.reasons))
        self.assertIn("暂无法读取B站等级", text)
        self.assertNotIn("等级不足", text)

    def test_v2_trace_and_aggregation_are_unchanged(self):
        stage = get_review_stages(GroupConfig(group_id="1", review_stages=[ReviewStageConfig(
            index=1, name="兼容组", conditions=["follow", "qq_level"],
        )]))[0]
        traces, result = execute(stage, {
            "follow": RuleConditionStatus("unknown", "接口不可用"),
            "qq_level": RuleConditionStatus("false", "QQ等级不足", observation=10),
        })
        self.assertEqual(result.outcome, "false")
        self.assertEqual(result.reasons, ["QQ等级不足"])
        self.assertEqual(traces, ["兼容组 | all_pass[follow,qq_level] | 未通过 | reject | QQ等级不足"])


if __name__ == "__main__":
    unittest.main()
