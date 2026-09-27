"""Pure chat-message formatting helpers."""

from __future__ import annotations

import re


CONDITION_CN_NAMES = {
    "bili_account": "B站账号可搜索",
    "bili_level": "B站等级",
    "qq_level": "QQ等级",
    "follow": "关注",
    "medal": "粉丝牌",
    "no_leave_record": "无退群记录",
    "no_conflict": "无冒用",
    "no_identity_change": "不更换B站账号",
}


def format_condition_details(
    condition_states: dict[str, str], condition_reasons: dict[str, str]
) -> list[str]:
    """Group condition states and show diagnostics for false/unknown items."""
    if not condition_states:
        return []

    passed = [
        CONDITION_CN_NAMES.get(name, name)
        for name, state in condition_states.items()
        if state == "true"
    ]
    failed: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    for name, state in condition_states.items():
        display_name = CONDITION_CN_NAMES.get(name, name)
        reason = condition_reasons.get(name, "")
        if state == "false":
            failed.append((display_name, reason or "未通过"))
        elif state == "unknown":
            unknown.append((display_name, reason or "无法判断"))

    lines: list[str] = []
    if passed:
        lines.append("通过：")
        lines.extend(f"  - {name}" for name in passed)
    if failed:
        lines.append("未通过：")
        for name, reason in failed:
            lines.append(f"  - {name}")
            lines.append(f"    原因：{reason}")
    if unknown:
        lines.append("无法判断：")
        for name, reason in unknown:
            lines.append(f"  - {name}")
            lines.append(f"    原因：{reason}")
    return lines


def format_condition_summary(audit) -> list[str]:
    return format_condition_details(audit.condition_states, audit.condition_reasons)


def _format_stage_result_lines(stage_line: str, number: int) -> list[str]:
    parts = stage_line.split(" | ", 4)
    if len(parts) != 5:
        return [f"{number}. {stage_line}"]

    stage_name, raw_rule, outcome, action, reason = parts
    mode, separator, raw_conditions = raw_rule.partition("[")
    condition_ids = raw_conditions.removesuffix("]").split(",") if separator else []
    condition_text = "、".join(
        CONDITION_CN_NAMES.get(name, name) for name in condition_ids if name
    ) or "未记录"
    mode_text = {"all_pass": "全部通过", "any_pass": "任一通过"}.get(mode, mode)
    action_text = {
        "approve": "通过申请",
        "reject": "拒绝申请",
        "ignore": "忽略申请",
        "next(顺序下一个分组)": "进入下一筛选组",
    }.get(action, action)
    next_match = re.fullmatch(r"next\(分组(\d+)\)", action)
    if next_match:
        action_text = f"进入筛选组 {next_match.group(1)}"

    return [
        f"{number}. {stage_name}",
        f"   规则：{mode_text}（{condition_text}）",
        f"   结果：{outcome}",
        f"   动作：{action_text}",
        f"   原因：{reason or '无'}",
    ]


def format_stage_results(stage_results: list[str]) -> list[str]:
    lines: list[str] = []
    for number, stage_line in enumerate(stage_results, start=1):
        if lines:
            lines.append("")
        lines.extend(_format_stage_result_lines(stage_line, number))
    return lines
