"""Pure configuration models and parsers, without loading application state."""

import json
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# 判定真值的标准字符串集合，供 parse_bool 统一使用
BOOL_TRUE_VALUES = {"1", "true", "yes", "on", "开启", "启用"}
ACTION_ALIASES = {
    "approve": "approve",
    "pass": "approve",
    "通过": "approve",
    "同意": "approve",
    "reject": "reject",
    "deny": "reject",
    "refuse": "reject",
    "拒绝": "reject",
    "ignore": "ignore",
    "skip": "ignore",
    "忽略": "ignore",
}
STAGE_FLOW_ACTIONS = {"next", "approve", "reject", "ignore"}
STAGE_SIDE_EFFECT_ACTIONS = {"notify_admin", "log_warn"}
STAGE_ACTIONS = STAGE_FLOW_ACTIONS | STAGE_SIDE_EFFECT_ACTIONS
REVIEW_CONDITIONS = {
    "bili_account",
    "bili_level",
    "qq_level",
    "follow",
    "medal",
    "no_leave_record",
    "no_conflict",
    "no_identity_change",
}
STAGE_MODES = {"all_pass", "any_pass"}
DEFAULT_STAGE_BRANCH_FLOWS = {
    "on_true": "next",
    "on_false": "reject",
    "on_unknown": "ignore",
}


def parse_bool(value: Any) -> bool:
    """将 bool/int/str 转换为布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in BOOL_TRUE_VALUES


def parse_uid_list(value: Any) -> list[int]:
    """将 list/tuple 或逗号分隔字符串转为整数 UID 列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[int] = []
        for item in value:
            if item in ("", None):
                continue
            result.append(int(item))
        return result
    return [int(item.strip()) for item in str(value).replace("，", ",").split(",") if item.strip()]


def parse_action_list(value: Any, allowed: set[str]) -> list[str]:
    """将逗号分隔字符串转换为动作列表。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    result: list[str] = []
    for item in items:
        normalized = str(item).strip().lower()
        normalized = ACTION_ALIASES.get(normalized, normalized)
        if not normalized:
            continue
        if normalized not in allowed:
            raise ValueError(f"invalid stage action: {item}")
        result.append(normalized)
    return result


def parse_named_stage_target(value: Any) -> Optional[str]:
    """将 YAML 中的命名分组跳转目标解析为分组 ID。"""
    if value in ("", None):
        return None
    target = str(value).strip()
    if not target:
        return None
    return target


def parse_str_list(value: Any) -> list[str]:
    """将 list/tuple 或逗号分隔字符串转为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace("，", ",").split(",") if item.strip()]


def parse_condition_list(value: Any) -> list[str]:
    """解析并校验分组条件列表。"""
    conditions = parse_str_list(value)
    if not conditions:
        raise ValueError("conditions 必须至少包含一个条件")
    unknown = [condition for condition in conditions if condition not in REVIEW_CONDITIONS]
    if unknown:
        raise ValueError(f"未知审核条件：{'、'.join(unknown)}")
    return list(dict.fromkeys(conditions))


def parse_custom_reject_uids(value: Any) -> dict[str, str]:
    """兼容 JSON/YAML 的自定义拒绝名单配置。"""
    if value in ("", None):
        return {}

    if not isinstance(value, dict):
        parsed = json.loads(str(value))
        if not isinstance(parsed, dict):
            raise ValueError("custom_reject_uids 必须是对象")
        value = parsed

    result: dict[str, str] = {}
    for uid, reason_config in value.items():
        uid_text = str(uid).strip()
        if not uid_text:
            continue

        if isinstance(reason_config, dict):
            reason = str(reason_config.get("reason") or "").strip()
        else:
            reason = str(reason_config).strip()

        if not reason:
            continue
        result[uid_text] = reason
    return result


def parse_reject_reasons(value: Any) -> dict[str, str]:
    """解析按条件配置的拒绝原因。"""
    if value in ("", None):
        return {}
    if not isinstance(value, dict):
        value = json.loads(str(value))
    if not isinstance(value, dict):
        raise ValueError("reject_reasons 必须是对象")
    return {
        str(condition).strip(): str(reason).strip()
        for condition, reason in value.items()
        if str(condition).strip() and str(reason).strip()
    }


class GroupConfig(BaseModel):
    """单个群的加群审核配置。"""

    group_id: str
    enabled: bool = True  # 是否启用加群管理
    required_qq_level: int = 0  # QQ 最低等级要求
    required_bili_level: int = 0  # B站最低等级要求
    target_uids: list[int] = Field(default_factory=list)  # 需要关注的 B 站 UID 列表
    target_medal_uids: list[int] = Field(default_factory=list)  # 需要检查粉丝牌的主播 UID 列表
    required_medal_level: int = 1  # 粉丝牌最低等级要求
    reject_reason_with_leave_time: bool = True  # 拒绝时是否附带退群时间
    custom_reject_uids: dict[str, str] = Field(default_factory=dict)  # 自定义拒绝名单，key=B站UID，value=拒绝理由
    reject_reasons: dict[str, str] = Field(default_factory=dict)  # 按条件覆盖拒绝原因
    review_pipeline_file: str = ""  # 审核分组 YAML 文件
    review_stages: list["ReviewStageConfig"] = Field(default_factory=list)  # 阶段化审核配置


class ReviewStageConfig(BaseModel):
    """单个审核阶段的配置。"""

    index: int
    name: str = ""
    conditions: list[str]
    mode: str = "all_pass"
    on_true: list[str] = Field(default_factory=lambda: ["next"])
    on_false: list[str] = Field(default_factory=lambda: ["reject"])
    on_unknown: list[str] = Field(default_factory=lambda: ["ignore"])
    on_true_target: Optional[int] = None
    on_false_target: Optional[int] = None
    on_unknown_target: Optional[int] = None
    on_true_reason: str = ""
    on_false_reason: str = ""
    on_unknown_reason: str = ""

    @model_validator(mode="after")
    def validate_actions(self):
        """确保每个结果分支都有且只有一个流转动作。"""
        for field_name in ("on_true", "on_false", "on_unknown"):
            actions = getattr(self, field_name)
            flow_actions = [action for action in actions if action in STAGE_FLOW_ACTIONS]
            if len(flow_actions) != 1:
                raise ValueError(f"{field_name} 必须且只能包含一个流转动作")
            target = getattr(self, f"{field_name}_target")
            if target is not None and flow_actions[0] != "next":
                raise ValueError(f"{field_name}_target 仅能与 next 流转动作一起使用")
        self.conditions = parse_condition_list(self.conditions)
        if self.mode not in STAGE_MODES:
            raise ValueError("stage mode 必须是 all_pass 或 any_pass")
        return self


GroupConfig.model_rebuild()
