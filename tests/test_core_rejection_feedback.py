"""Applicant-facing explanations preserve facts, configured text and unknown states."""

import unittest
from types import SimpleNamespace

from src.gatekeeper.conditions import LEGACY_REJECT_REASONS, build_rule_condition_statuses
from src.gatekeeper.config import GroupConfig
from src.gatekeeper.formatters import build_reject_reason, build_stage_reject_reasons, format_uid_restriction_reason
from src.gatekeeper.models import CheckResult, RuleConditionStatus, StageRuntime


class RejectionFeedbackTests(unittest.TestCase):
    def group(self, **overrides):
        return GroupConfig(
            group_id="1", required_bili_level=2, required_qq_level=4,
            target_uids=[11, 22], target_medal_uids=[33, 44], required_medal_level=5,
            reject_reason_with_leave_time=False, **overrides,
        )

    def statuses(self, conditions, *, group=None, **overrides):
        values = dict(
            qq_level=3, bili_search_result=CheckResult("passed", matched_target_uid=123), bili_level=1,
            follow_result=CheckResult("failed"), medal_result=CheckResult("failed"),
            leave_record=SimpleNamespace(leave_time="2026-10-17T08:09:10"), conflict_qq="private-other-qq",
            owned_other_bili_uids=[777], conflict_lookup_known=True, identity_change_lookup_known=True,
        )
        values.update(overrides)
        return build_rule_condition_statuses(group or self.group(), set(conditions), **values)

    def stage(self, conditions):
        return StageRuntime(1, "入群要求", list(conditions), "all_pass", ["approve"], ["reject"], ["reject"])

    def test_each_negative_condition_explains_requirements_and_a_next_step(self):
        cases = {
            "bili_level": ("当前1级", "至少2级", "重新申请"),
            "qq_level": ("当前3级", "至少4级", "重新申请"),
            "follow": ("11、22", "任一位", "请关注"),
            "medal": ("33、44", "至少5级", "核对账号"),
            "no_leave_record": ("本群的退群记录", "联系管理员"),
            "no_conflict": ("另一个QQ绑定", "核对", "联系管理员"),
            "no_identity_change": ("777", "原绑定账号", "联系管理员"),
        }
        for condition, fragments in cases.items():
            with self.subTest(condition=condition):
                status = self.statuses([condition])[condition]
                self.assertEqual(status.state, "false")
                for fragment in fragments:
                    self.assertIn(fragment, status.reason)
                self.assertNotIn("疑似", status.reason)
                self.assertNotIn("private-other-qq", status.reason)

    def test_custom_text_is_kept_with_the_actual_measurement(self):
        group = self.group(reject_reasons={"qq_level": "本群等级要求较高"})
        reason = self.statuses(["qq_level"], group=group)["qq_level"].reason
        self.assertIn("本群等级要求较高", reason)
        self.assertIn("当前3级", reason)
        self.assertIn("至少4级", reason)

    def test_legacy_defaults_are_replaced_without_changing_configuration(self):
        for condition, texts in LEGACY_REJECT_REASONS.items():
            for text in texts:
                with self.subTest(condition=condition, text=text):
                    group = self.group(reject_reasons={condition: text})
                    before = group.model_dump()
                    overrides = {"bili_search_result": CheckResult("failed", "UID不存在，请检查UID后重新申请")} if condition == "bili_account" else {}
                    reason = self.statuses([condition], group=group, **overrides)[condition].reason
                    self.assertNotEqual(reason, text)
                    self.assertNotIn("疑似", reason)
                    self.assertEqual(group.model_dump(), before)

    def test_leave_timestamp_respects_visibility_setting_and_missing_values(self):
        hidden = self.statuses(["no_leave_record"])["no_leave_record"].reason
        self.assertNotIn("2026", hidden)
        group = self.group()
        group.reject_reason_with_leave_time = True
        visible = self.statuses(["no_leave_record"], group=group)["no_leave_record"].reason
        self.assertIn("2026-10-17 08:09:10", visible)
        empty = self.statuses(["no_leave_record"], group=group, leave_record=SimpleNamespace(leave_time=""))["no_leave_record"].reason
        self.assertIn("退群记录", empty)

    def test_custom_stage_cannot_hide_any_failed_check(self):
        statuses = self.statuses(["bili_level", "qq_level"])
        reasons = build_stage_reject_reasons(self.stage(statuses), "false", statuses, ["本群暂不接受"])
        text = build_reject_reason(reasons)
        self.assertIn("本群暂不接受", text)
        self.assertIn("当前1级", text)
        self.assertIn("当前3级", text)
        self.assertIn("至少2级", text)
        self.assertIn("至少4级", text)

    def test_other_stage_failures_do_not_become_this_stages_reason(self):
        statuses = self.statuses(["bili_level", "qq_level"])
        reasons = build_stage_reject_reasons(self.stage(["qq_level"]), "false", statuses, ["自定义"])
        text = build_reject_reason(reasons)
        self.assertIn("当前3级", text)
        self.assertNotIn("当前1级", text)

    def test_unknown_rejection_explains_inability_to_verify(self):
        statuses = self.statuses(["follow"], follow_result=CheckResult("inaccessible", "关注列表不可见"))
        self.assertEqual(statuses["follow"].state, "unknown")
        reasons = build_stage_reject_reasons(self.stage(["follow"]), "unknown", statuses, ["暂不接受"])
        text = build_reject_reason(reasons)
        self.assertIn("关注列表不可见", text)
        self.assertIn("未能完成", text)
        self.assertIn("稍后重试", text)
        self.assertNotIn("未检测到该B站账号关注", text)
        self.assertNotIn("按配置忽略", text)

    def test_unknown_without_diagnostic_does_not_claim_a_failed_condition(self):
        reasons = build_stage_reject_reasons(
            self.stage(["follow"]), "unknown", {"follow": RuleConditionStatus("unknown")}, []
        )
        self.assertIn("暂无法核验", "；".join(reasons))
        self.assertNotIn("未满足本群要求", "；".join(reasons))

    def test_true_branch_rejection_explains_the_policy(self):
        reasons = build_stage_reject_reasons(
            self.stage(["bili_account"]), "true", {"bili_account": RuleConditionStatus("true")}, ["管理员策略"]
        )
        text = build_reject_reason(reasons)
        self.assertIn("管理员策略", text)
        self.assertIn("拒绝规则", text)
        self.assertIn("已满足条件", text)
        self.assertNotIn("等级不足", text)

    def test_reason_is_complete_and_deduplicated_even_above_120_characters(self):
        long_text = "管理员补充说明" * 40
        tail = "请不要填写B站昵称或UID以外的内容"
        text = build_reject_reason([long_text, tail, tail, " "])
        self.assertIn(long_text, text)
        self.assertTrue(text.endswith(tail))
        self.assertEqual(text.count(tail), 1)
        self.assertGreater(len(text), 120)
        self.assertIn("联系管理员", build_reject_reason([]))

    def test_restriction_list_explains_the_cause_and_how_to_request_review(self):
        for configured in (" ", "管理员设置的限制原因"):
            reason = format_uid_restriction_reason(configured)
            self.assertIn("本群限制名单", reason)
            self.assertIn("联系管理员", reason)
            if configured.strip():
                self.assertIn(configured, reason)


if __name__ == "__main__":
    unittest.main()
