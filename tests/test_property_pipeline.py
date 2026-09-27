"""T/F/I 管道与解析函数的属性测试（Hypothesis 自动生成边界用例）。"""

import unittest

import nonebot

nonebot.init()

from hypothesis import given, settings
from hypothesis import strategies as st

from src.plugins.group_request_manager.main import (
    RuleConditionStatus,
    StageRuntime,
    _evaluate_stage_conditions,
    _extract_bili_name,
    _parse_qq_level,
)

# 纯函数用例，关闭单例耗时上限以适配 CI 慢节点
settings.register_profile("gatekeeper", max_examples=50, deadline=None)
settings.load_profile("gatekeeper")

STATES = st.sampled_from(["true", "false", "unknown"])

ANSWER_PREFIXES = ("答案：", "答案:", "answer：", "answer:")

# str.splitlines() 的全部行边界字符（不止 \n\r，还包括 \x0c 换页符等）
SPLITLINE_CHARS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def _no_line_breaks(value: str) -> bool:
    return not any(char in SPLITLINE_CHARS for char in value)


def _make_stage(states, mode):
    names = [f"condition_{index}" for index in range(len(states))]
    statuses = {
        name: RuleConditionStatus(state=state) for name, state in zip(names, states)
    }
    stage = StageRuntime(
        index=1,
        name="测试分组",
        conditions=names,
        mode=mode,
        on_true=["ignore"],
        on_false=["ignore"],
        on_unknown=["ignore"],
    )
    return stage, statuses


class EvaluateStageConditionProperties(unittest.TestCase):
    @given(states=st.lists(STATES, max_size=10))
    def test_all_pass_mode_priority(self, states):
        stage, statuses = _make_stage(states, "all_pass")
        outcome = _evaluate_stage_conditions(stage, statuses)
        self.assertIn(outcome, {"true", "false", "unknown"})
        if "false" in states:
            self.assertEqual(outcome, "false")
        elif "unknown" in states:
            self.assertEqual(outcome, "unknown")
        else:
            self.assertEqual(outcome, "true")

    @given(states=st.lists(STATES, max_size=10))
    def test_any_pass_mode_priority(self, states):
        stage, statuses = _make_stage(states, "any_pass")
        outcome = _evaluate_stage_conditions(stage, statuses)
        self.assertIn(outcome, {"true", "false", "unknown"})
        if "true" in states:
            self.assertEqual(outcome, "true")
        elif "unknown" in states:
            self.assertEqual(outcome, "unknown")
        else:
            self.assertEqual(outcome, "false")

    @given(states=st.lists(STATES, max_size=10))
    def test_unknown_condition_never_yields_true(self, states):
        """业务红线：存在无法判断的条件时，任何汇总模式都不允许直接给出 true。"""
        for mode in ("all_pass", "any_pass"):
            stage, statuses = _make_stage(states, mode)
            outcome = _evaluate_stage_conditions(stage, statuses)
            if "unknown" in states and "true" not in states:
                self.assertNotEqual(outcome, "true")


class ParseQqLevelProperties(unittest.TestCase):
    @given(st.integers(min_value=0))
    def test_non_negative_int_kept(self, value):
        self.assertEqual(_parse_qq_level(value), value)

    @given(st.integers(max_value=-1))
    def test_negative_int_unreadable(self, value):
        self.assertIsNone(_parse_qq_level(value))

    @given(st.integers(max_value=-1))
    def test_negative_number_in_string_unreadable(self, value):
        self.assertIsNone(_parse_qq_level(str(value)))

    @given(st.booleans())
    def test_bool_unreadable(self, value):
        self.assertIsNone(_parse_qq_level(value))

    @given(
        st.text(max_size=20).filter(
            lambda value: not any(char.isdigit() for char in value)
        )
    )
    def test_digit_free_text_unreadable(self, value):
        self.assertIsNone(_parse_qq_level(value))

    @given(
        prefix=st.sampled_from(["", "Lv", "等级", "lv.", "QQ"]),
        level=st.integers(min_value=0, max_value=256),
    )
    def test_level_number_extracted(self, prefix, level):
        self.assertEqual(_parse_qq_level(f"{prefix}{level}"), level)


class ExtractBiliNameProperties(unittest.TestCase):
    @given(
        st.text(max_size=30).filter(
            lambda value: _no_line_breaks(value)
            and not any(value.startswith(prefix) for prefix in ANSWER_PREFIXES)
        )
    )
    def test_answer_line_value_extracted(self, value):
        self.assertEqual(_extract_bili_name(f"问题：xxx\n答案：{value}"), value.strip())

    @given(st.text(max_size=30).filter(_no_line_breaks))
    def test_plain_comment_returned_stripped(self, value):
        self.assertEqual(_extract_bili_name(value), value.strip())


if __name__ == "__main__":
    unittest.main()
