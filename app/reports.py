"""Formatting for the /检查qq administrator report."""

from __future__ import annotations

from src.gatekeeper.formatters import format_condition_summary, format_stage_results
from app.database.records import JoinRequestAudit, UserAuditSummary, UserBinding
from src.gatekeeper.parsing import parse_bili_identity


def needs_profile_name(name: str, uid: int | None) -> bool:
    """Identify only empty names or old raw-UID placeholders for this exact UID."""
    if uid is None:
        return False
    if not name:
        return True
    identity = parse_bili_identity(name)
    return (identity.kind == "uid" and len(identity.value) <= 20 and identity.value.isascii()
            and identity.value.isdigit() and int(identity.value) == uid)


def format_user_check_reply(
    qq: str,
    binding: UserBinding | None,
    summary: UserAuditSummary,
    audits: list[JoinRequestAudit],
    *,
    resolved_names: dict[int, str] | None = None,
) -> str:
    if not binding and summary.attempt_count == 0:
        return f"未找到 QQ {qq} 的加群申请记录"

    resolved_names = resolved_names or {}

    def display_name(name, uid):
        if needs_profile_name(name, uid):
            return resolved_names.get(uid, "未记录")
        if not name or (uid is None and parse_bili_identity(name).kind == "uid"):
            return "未记录"
        return name

    lines = ["【QQ 用户检查】", "", f"QQ：{qq}", "", "【绑定与统计】"]
    binding_groups = list(binding.groups) if binding else []
    merged_groups = list(dict.fromkeys(binding_groups + summary.groups))
    latest_bili_name = binding.latest_bili_name if binding else ""
    latest_bili_uid = binding.latest_bili_uid if binding else None
    if summary.attempt_count:
        attempt_count = summary.attempt_count
        approved_count = summary.approved_count
        rejected_count = summary.rejected_count
        ignored_count = summary.ignored_count
    else:
        attempt_count = binding.attempt_count if binding else 0
        approved_count = binding.approved_count if binding else 0
        rejected_count = binding.rejected_count if binding else 0
        ignored_count = binding.ignored_count if binding else 0
    lines.extend(
        [
            f"最近绑定的 B站昵称：{display_name(latest_bili_name, latest_bili_uid)}",
            f"最近绑定的 B站 UID：{latest_bili_uid or '未记录'}",
            f"申请次数：{attempt_count}",
            (
                "通过 / 拒绝 / 忽略："
                f"{approved_count} / {rejected_count} / {ignored_count}"
            ),
            f"涉及群：{'、'.join(merged_groups) or '未记录'}",
        ]
    )
    if summary.pending_count:
        lines.append(f"状态待确认：{summary.pending_count}")

    if audits:
        lines.extend(["", "【最近申请记录】"])
        for record_number, audit in enumerate(audits, start=1):
            lines.extend(["", f"记录 {record_number}"])
            reason_text = "；".join(audit.reasons) or "无"
            result_text = {
                "approved": "通过",
                "rejected": "拒绝",
                "ignored": "忽略",
            }.get(audit.result, audit.result)
            action_status = getattr(audit, "action_status", "applied")
            if action_status == "pending":
                result_text = f"{result_text}（等待确认）"
            elif action_status == "failed":
                result_text = f"{result_text}（QQ 操作失败）"
            lines.extend(
                [
                    f"时间：{audit.created_at}",
                    f"群号：{audit.group_id}",
                    f"结果：{result_text}",
                    f"B站昵称：{display_name(audit.bili_name, audit.bili_uid)}",
                    f"B站 UID：{audit.bili_uid or '未记录'}",
                    *([f"申请填写：{audit.bili_name}"] if audit.bili_name and
                      (needs_profile_name(audit.bili_name, audit.bili_uid) or
                       (audit.bili_uid is None and parse_bili_identity(audit.bili_name).kind == "uid")) else []),
                    f"原因：{reason_text}",
                    "",
                    "条件结果",
                ]
            )
            lines.extend(format_condition_summary(audit))
            if audit.stage_results:
                lines.extend(["", "流程详情"])
                lines.extend(format_stage_results(audit.stage_results))
    if resolved_names:
        lines.extend(["", "说明：旧记录中的昵称按UID查询补全；原始审计和审核结果未修改。"])
    return "\n".join(lines)
