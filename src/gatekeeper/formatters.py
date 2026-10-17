"""Pure chat-message formatting helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import GroupConfig
    from .models import RuleConditionStatus, StageRuntime


CONDITION_CN_NAMES = {
    "bili_account": "B站账号确认",
    "bili_level": "B站等级",
    "qq_level": "QQ等级",
    "follow": "关注",
    "medal": "粉丝牌",
    "no_leave_record": "无退群记录",
    "no_conflict": "B站账号归属校验",
    "no_identity_change": "不更换B站账号",
}


def format_condition_details(condition_states: dict[str, str], condition_reasons: dict[str, str]) -> list[str]:
    """Group condition states and show diagnostics for false/unknown items."""
    if not condition_states:
        return []

    passed = [CONDITION_CN_NAMES.get(name, name) for name, state in condition_states.items() if state == "true"]
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
    condition_text = "、".join(CONDITION_CN_NAMES.get(name, name) for name in condition_ids if name) or "未记录"
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


def format_bool_text(value: bool) -> str:
    """将布尔值格式化为更易读的中文文本。"""
    return "开启" if value else "关闭"


def format_action_text(value: str) -> str:
    """将动作配置格式化为中文文本。"""
    return {
        "approve": "同意",
        "reject": "拒绝",
        "ignore": "忽略",
    }.get(value, value or "未设置")


def format_flow_action_text(flow_action: str, target_stage_index: int | None = None) -> str:
    """将阶段流转动作格式化为带跳转信息的文本。"""
    if flow_action != "next":
        return flow_action
    if target_stage_index is None:
        return "next(顺序下一个分组)"
    return f"next(分组{target_stage_index})"


def format_stage_pipeline_text(group_config: GroupConfig) -> str:
    """将阶段化审核配置格式化为便于查看的文本。"""
    if not group_config.review_stages:
        return "未配置审核分组管道"

    lines: list[str] = []
    for stage in sorted(group_config.review_stages, key=lambda item: item.index):
        condition_text = "、".join(CONDITION_CN_NAMES.get(name, name) for name in stage.conditions)
        mode_text = "全部通过" if stage.mode == "all_pass" else "任一通过"
        if lines:
            lines.append("")
        lines.extend(
            [
                f"{stage.index}. {stage.name or f'阶段{stage.index}'}",
                f"   规则：{mode_text}（{condition_text}）",
                f"   通过：{format_branch_action_display(stage.on_true, stage.on_true_target)}",
                f"   未通过：{format_branch_action_display(stage.on_false, stage.on_false_target)}",
                f"   无法判断：{format_branch_action_display(stage.on_unknown, stage.on_unknown_target)}",
            ]
        )
    return "\n".join(lines)


def format_branch_action_text(actions: list[str], target_stage_index: int | None) -> str:
    """将动作列表格式化为便于阅读的分支文本。"""
    rendered: list[str] = []
    for action in actions:
        if action in {"next", "approve", "reject", "ignore"}:
            rendered.append(format_flow_action_text(action, target_stage_index))
        else:
            rendered.append(action)
    return ",".join(rendered)


def format_branch_action_display(actions: list[str], target_stage_index: int | None) -> str:
    """将配置中的流程动作转成面向管理员的中文说明。"""
    labels: list[str] = []
    for action in actions:
        if action == "next":
            labels.append(f"进入筛选组 {target_stage_index}" if target_stage_index is not None else "进入下一筛选组")
        else:
            labels.append(
                {
                    "approve": "通过申请",
                    "reject": "拒绝申请",
                    "ignore": "忽略申请",
                }.get(action, action)
            )
    return "、".join(labels) or "无动作"


def format_group_config(group_config: GroupConfig) -> str:
    """将单个群配置格式化为适合聊天窗口阅读的文本。"""
    target_uids = "、".join(str(uid) for uid in group_config.target_uids) or "未设置"
    target_medal_uids = "、".join(str(uid) for uid in group_config.target_medal_uids) or "未设置"
    return "\n".join(
        [
            f"【群配置 {group_config.group_id}】",
            "",
            "基础设置",
            f"启用状态：{format_bool_text(group_config.enabled)}",
            f"QQ最低等级：{group_config.required_qq_level}",
            f"B站最低等级：{group_config.required_bili_level}",
            "",
            "目标设置",
            f"关注目标 UID：{target_uids}",
            f"粉丝牌目标 UID：{target_medal_uids}",
            f"粉丝牌最低等级：{group_config.required_medal_level}",
            f"拒绝时附带退群时间：{format_bool_text(group_config.reject_reason_with_leave_time)}",
            "",
            "审核流程",
            format_stage_pipeline_text(group_config),
        ]
    )


def build_stage_reject_reasons(
    stage: StageRuntime,
    outcome: str,
    statuses: dict[str, RuleConditionStatus],
    reasons: list[str],
) -> list[str]:
    """Explain the rejecting stage without hiding observations behind custom text."""
    result = list(dict.fromkeys(reason.strip() for reason in reasons if reason.strip()))
    for condition in stage.conditions:
        status = statuses.get(condition)
        if status is None or status.state != outcome or outcome not in {"false", "unknown"}:
            continue
        detail = status.reason.strip() or (
            "暂无法核验该项条件，请稍后重试或联系管理员" if outcome == "unknown"
            else "未满足本群要求，请联系管理员核实"
        )
        if not any(detail in reason for reason in result):
            result.append(f"{CONDITION_CN_NAMES.get(condition, condition)}：{detail}")
    if outcome == "unknown" and stage.conditions:
        result.append(f"本次未能完成“{stage.name or '审核阶段'}”的核验，按本群规则暂不接受申请；请稍后重试或联系管理员")
    elif outcome == "true":
        matched = "、".join(
            CONDITION_CN_NAMES.get(name, name) for name in stage.conditions
            if name in statuses and statuses[name].state == "true"
        )
        result.append(f"已命中本群“{stage.name or '审核阶段'}”的拒绝规则（已满足条件：{matched or '本阶段条件'}）；如需复核，请联系管理员")
    return result or ["本次申请未满足本群入群规则，请联系管理员核实具体原因"]


def format_uid_restriction_reason(configured_reason: str) -> str:
    detail = configured_reason.strip()
    parts = ["该B站账号命中本群限制名单，暂不能加入"]
    if detail:
        parts.append(f"管理员说明：{detail}")
    parts.append("如需复核，请联系管理员")
    return "；".join(parts)


def build_reject_reason(reasons: list[str]) -> str:
    """Send complete explanations; never silently cut a cause or correction hint."""
    unique = dict.fromkeys(reason.strip() for reason in reasons if reason.strip())
    text = "；".join(unique) or "本次申请未满足本群入群规则，请联系管理员核实具体原因"
    return f"来自BOT:{text}"


def format_stage_trace(
    stage: StageRuntime,
    outcome: str,
    actions: list[str],
    reasons: list[str],
) -> str:
    """记录阶段执行轨迹。"""
    target = (
        stage.on_true_target
        if outcome == "true"
        else stage.on_false_target
        if outcome == "false"
        else stage.on_unknown_target
    )
    outcome_cn = {"true": "通过", "false": "未通过", "unknown": "无法判断"}.get(outcome, outcome)
    action_text = format_branch_action_text(actions, target) or "无动作"
    reason_text = "；".join(reasons) or "无"
    condition_text = ",".join(stage.conditions)
    return f"{stage.name} | {stage.mode}[{condition_text}] | {outcome_cn} | {action_text} | {reason_text}"


def format_leave_time(leave_time: str) -> str:
    """将存量 ISO 时间统一转成更适合聊天展示的格式。"""
    if not leave_time:
        return ""
    normalized = leave_time.strip()
    for candidate in (normalized, normalized.replace("T", " ")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return normalized.replace("T", " ")
