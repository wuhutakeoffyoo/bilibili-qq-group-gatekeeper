"""One-time legacy configuration compiler and compatible YAML parsers.

The loader preserves V2's actual merge order. app.config calls it only while the
SQL configuration head is empty; all new configuration writes go to SQLite.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from app.settings import get_settings
from pydantic import BaseModel, ConfigDict, Field
import yaml

# Compatibility exports: existing callers can keep using the plugin path.
from src.gatekeeper.config import (  # noqa: F401
    ACTION_ALIASES,
    BOOL_TRUE_VALUES,
    DEFAULT_STAGE_BRANCH_FLOWS,
    REVIEW_CONDITIONS,
    STAGE_ACTIONS,
    STAGE_FLOW_ACTIONS,
    STAGE_MODES,
    STAGE_SIDE_EFFECT_ACTIONS,
    GroupConfig,
    ReviewStageConfig,
    parse_action_list,
    parse_bool,
    parse_condition_list,
    parse_custom_reject_uids,
    parse_named_stage_target,
    parse_reject_reasons,
    parse_str_list,
    parse_uid_list,
)

logger = logging.getLogger("group_request_manager.config")

# 运行时状态文件的存放路径
DATA_PATH = Path("data/runtime")
CONFIG_ROOT = Path.cwd()
SOURCE_PATHS: set[Path] = set()
CONFIG_FILE = DATA_PATH / "plugin_state.json"
GROUP_OVERRIDES_FILE = DATA_PATH / "group_overrides.json"
LEGACY_CONFIG_FILE = Path("data/config/plugin_config.json")


def resolve_config_path(value: Any, base_dir: Optional[Path] = None) -> Path:
    """将环境变量中的路径解析为绝对路径。"""
    raw = str(value).strip()
    if not raw:
        raise ValueError("config path is empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base_dir or CONFIG_ROOT) / path
    return path


# ---------------------------------------------------------------------------
# YAML 配置加载
# ---------------------------------------------------------------------------


def _normalize_yaml_groups(raw_groups: Any) -> list[dict[str, Any]]:
    """兼容 YAML 中 groups 为列表或对象两种写法。"""
    if isinstance(raw_groups, list):
        result: list[dict[str, Any]] = []
        for item in raw_groups:
            if not isinstance(item, dict):
                raise ValueError("YAML groups 列表中的每一项都必须是对象")
            result.append(dict(item))
        return result

    if isinstance(raw_groups, dict):
        result = []
        for group_id, item in raw_groups.items():
            if not isinstance(item, dict):
                raise ValueError("YAML groups 对象中的每个分组都必须是对象")
            merged = dict(item)
            merged.setdefault("id", group_id)
            result.append(merged)
        return result

    raise ValueError("YAML groups 必须是列表或对象")


def _parse_yaml_branch(
    value: Any,
    *,
    branch_name: str,
    default_flow: str,
) -> tuple[list[str], Optional[str], str]:
    """解析 YAML 中某个结果分支的配置。"""
    if value in ("", None):
        return [default_flow], None, ""

    if isinstance(value, dict):
        flow = str(value.get("flow", default_flow)).strip().lower() or default_flow
        if flow not in STAGE_FLOW_ACTIONS:
            raise ValueError(f"{branch_name}.flow 必须是 next / approve / reject / ignore")

        effects = parse_action_list(value.get("effects", []), STAGE_SIDE_EFFECT_ACTIONS)
        target = parse_named_stage_target(value.get("goto", value.get("target")))
        reason = str(value.get("reason") or "").strip()
        if target is not None and flow != "next":
            raise ValueError(f"{branch_name}.goto 仅能与 next 流转动作一起使用")
        return [flow, *effects], target, reason

    actions = parse_action_list(value, STAGE_ACTIONS)
    return actions, None, ""


def load_review_stages_from_yaml_file(
    path_value: Any,
    *,
    base_dir: Optional[Path] = None,
) -> list[ReviewStageConfig]:
    """从 YAML 文件加载审核分组，并编译为现有的 review_stages。"""
    config_path = resolve_config_path(path_value, base_dir)
    SOURCE_PATHS.add(config_path.resolve())
    if not config_path.exists():
        raise ValueError(f"审核 YAML 配置文件不存在：{config_path}")

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读取审核 YAML 配置文件失败：{config_path}") from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"解析审核 YAML 配置文件失败：{config_path}") from exc

    if not isinstance(data, dict):
        raise ValueError("审核 YAML 的根节点必须是对象")

    if int(data.get("version", 0)) not in {2, 3}:
        raise ValueError("审核 YAML 仅支持 version: 2 或 3")

    raw_groups = data.get("groups")
    if raw_groups in ("", None):
        raise ValueError("审核 YAML 缺少 groups 配置")

    groups = _normalize_yaml_groups(raw_groups)
    if not groups:
        raise ValueError("审核 YAML 的 groups 不能为空")

    entry_group_id = parse_named_stage_target(data.get("entry"))
    parsed_groups: list[dict[str, Any]] = []
    original_order: list[str] = []
    existing_ids: set[str] = set()

    for item in groups:
        group_id = parse_named_stage_target(item.get("id"))
        if group_id is None:
            raise ValueError("审核 YAML 的每个分组都必须包含非空 id")
        if group_id in existing_ids:
            raise ValueError(f"审核 YAML 中分组 ID 重复：{group_id}")

        when = str(item.get("when") or "").strip()
        try:
            conditions = parse_condition_list(item.get("conditions")) if not when else []
        except ValueError as exc:
            raise ValueError(f"审核 YAML 分组 {group_id} 的 conditions 无效：{exc}") from exc
        mode = str(item.get("mode") or "all_pass").strip().lower()
        if mode not in STAGE_MODES:
            raise ValueError(f"审核 YAML 分组 {group_id} 的 mode 必须是 all_pass 或 any_pass")

        on_true, on_true_target, on_true_reason = _parse_yaml_branch(
            item.get("on_true"),
            branch_name=f"groups.{group_id}.on_true",
            default_flow=DEFAULT_STAGE_BRANCH_FLOWS["on_true"],
        )
        on_false, on_false_target, on_false_reason = _parse_yaml_branch(
            item.get("on_false"),
            branch_name=f"groups.{group_id}.on_false",
            default_flow=DEFAULT_STAGE_BRANCH_FLOWS["on_false"],
        )
        on_unknown, on_unknown_target, on_unknown_reason = _parse_yaml_branch(
            item.get("on_unknown"),
            branch_name=f"groups.{group_id}.on_unknown",
            default_flow=DEFAULT_STAGE_BRANCH_FLOWS["on_unknown"],
        )

        parsed_groups.append(
            {
                "id": group_id,
                "name": str(item.get("name") or "").strip(),
                "conditions": conditions,
                "mode": mode,
                "when": when,
                "on_true": on_true,
                "on_false": on_false,
                "on_unknown": on_unknown,
                "on_true_target": on_true_target,
                "on_false_target": on_false_target,
                "on_unknown_target": on_unknown_target,
                "on_true_reason": on_true_reason,
                "on_false_reason": on_false_reason,
                "on_unknown_reason": on_unknown_reason,
            }
        )
        original_order.append(group_id)
        existing_ids.add(group_id)

    if entry_group_id is not None and entry_group_id not in existing_ids:
        raise ValueError(f"审核 YAML 的 entry 指向不存在的分组：{entry_group_id}")

    for group in parsed_groups:
        for branch_name in ("on_true_target", "on_false_target", "on_unknown_target"):
            target = group[branch_name]
            if target is None:
                continue
            if target not in existing_ids:
                raise ValueError(
                    f"审核 YAML 分组 {group['id']} 的 {branch_name} 指向不存在的分组：{target}"
                )

    ordered_group_ids = original_order
    if entry_group_id is not None:
        ordered_group_ids = [entry_group_id] + [
            group_id for group_id in original_order if group_id != entry_group_id
        ]

    index_by_group_id = {
        group_id: index for index, group_id in enumerate(ordered_group_ids, start=1)
    }
    group_map = {group["id"]: group for group in parsed_groups}

    return [
        ReviewStageConfig(
            index=index_by_group_id[group_id],
            name=group_map[group_id]["name"],
            conditions=group_map[group_id]["conditions"],
            mode=group_map[group_id]["mode"],
            when=group_map[group_id]["when"],
            on_true=group_map[group_id]["on_true"],
            on_false=group_map[group_id]["on_false"],
            on_unknown=group_map[group_id]["on_unknown"],
            on_true_target=(
                index_by_group_id[group_map[group_id]["on_true_target"]]
                if group_map[group_id]["on_true_target"] is not None
                else None
            ),
            on_false_target=(
                index_by_group_id[group_map[group_id]["on_false_target"]]
                if group_map[group_id]["on_false_target"] is not None
                else None
            ),
            on_unknown_target=(
                index_by_group_id[group_map[group_id]["on_unknown_target"]]
                if group_map[group_id]["on_unknown_target"] is not None
                else None
            ),
            on_true_reason=group_map[group_id]["on_true_reason"],
            on_false_reason=group_map[group_id]["on_false_reason"],
            on_unknown_reason=group_map[group_id]["on_unknown_reason"],
        )
        for group_id in ordered_group_ids
    ]


def _normalize_group_config_entries(raw_groups: Any) -> list[dict[str, Any]]:
    """兼容 groups.yaml 中 groups 为列表或对象两种写法。"""
    if isinstance(raw_groups, list):
        result: list[dict[str, Any]] = []
        for item in raw_groups:
            if not isinstance(item, dict):
                raise ValueError("groups.yaml 的 groups 列表中的每一项都必须是对象")
            result.append(dict(item))
        return result

    if isinstance(raw_groups, dict):
        result = []
        for group_id, item in raw_groups.items():
            if not isinstance(item, dict):
                raise ValueError("groups.yaml 的 groups 对象中的每个群配置都必须是对象")
            merged = dict(item)
            merged.setdefault("group_id", group_id)
            result.append(merged)
        return result

    raise ValueError("groups.yaml 的 groups 必须是列表或对象")


def _apply_mapping_group_fields(
    group_config: GroupConfig,
    raw_config: dict[str, Any],
    *,
    base_dir: Optional[Path] = None,
) -> None:
    """将 YAML/JSON 对象中的群配置写入 GroupConfig。"""
    enabled = raw_config.get("enabled")
    if enabled not in ("", None):
        group_config.enabled = parse_bool(enabled)

    required_qq_level = raw_config.get("required_qq_level")
    if required_qq_level not in ("", None):
        group_config.required_qq_level = int(required_qq_level)

    required_bili_level = raw_config.get("required_bili_level")
    if required_bili_level not in ("", None):
        group_config.required_bili_level = int(required_bili_level)

    target_uids = raw_config.get("target_uids")
    if target_uids not in ("", None):
        group_config.target_uids = parse_uid_list(target_uids)

    target_medal_uids = raw_config.get("target_medal_uids")
    if target_medal_uids not in ("", None):
        group_config.target_medal_uids = parse_uid_list(target_medal_uids)

    required_medal_level = raw_config.get("required_medal_level")
    if required_medal_level not in ("", None):
        group_config.required_medal_level = int(required_medal_level)

    reject_reason_with_leave_time = raw_config.get("reject_reason_with_leave_time")
    if reject_reason_with_leave_time not in ("", None):
        group_config.reject_reason_with_leave_time = parse_bool(
            reject_reason_with_leave_time
        )

    custom_reject_uids = raw_config.get("custom_reject_uids")
    if custom_reject_uids not in ("", None):
        group_config.custom_reject_uids = parse_custom_reject_uids(custom_reject_uids)

    reject_reasons = raw_config.get("reject_reasons")
    if reject_reasons not in ("", None):
        group_config.reject_reasons = parse_reject_reasons(reject_reasons)

    for field_name in ("verification", "patrol"):
        if field_name in raw_config and field_name in GroupConfig.model_fields:
            value = raw_config[field_name]
            if not isinstance(value, dict):
                raise ValueError(f"{field_name} 必须是对象")
            setattr(group_config, field_name, dict(value))
    if "release_binding_on_leave" in raw_config and "release_binding_on_leave" in GroupConfig.model_fields:
        group_config.release_binding_on_leave = parse_bool(raw_config["release_binding_on_leave"])

    review_pipeline_file = raw_config.get("review_pipeline_file")
    if review_pipeline_file not in ("", None):
        resolved_pipeline_file = resolve_config_path(review_pipeline_file, base_dir).resolve()
        group_config.review_pipeline_file = str(resolved_pipeline_file)
        group_config.review_stages = load_review_stages_from_yaml_file(
            resolved_pipeline_file,
        )


def load_group_configs_from_yaml_file(path_value: Any) -> list[GroupConfig]:
    """从 groups.yaml 加载多个群的基础配置。"""
    config_path = resolve_config_path(path_value)
    SOURCE_PATHS.add(config_path.resolve())
    if not config_path.exists():
        raise ValueError(f"群配置 YAML 文件不存在：{config_path}")

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读取群配置 YAML 文件失败：{config_path}") from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"解析群配置 YAML 文件失败：{config_path}") from exc

    if not isinstance(data, dict):
        raise ValueError("群配置 YAML 的根节点必须是对象")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("群配置 YAML 的 defaults 必须是对象")

    raw_groups = data.get("groups")
    if raw_groups in ("", None):
        raise ValueError("群配置 YAML 缺少 groups 配置")

    groups = _normalize_group_config_entries(raw_groups)
    if not groups:
        raise ValueError("群配置 YAML 的 groups 不能为空")

    result: list[GroupConfig] = []
    existing_group_ids: set[str] = set()
    base_dir = config_path.parent

    for item in groups:
        group_id = str(item.get("group_id", item.get("id", ""))).strip()
        if not group_id:
            raise ValueError("群配置 YAML 的每个群都必须包含非空 group_id")
        if group_id in existing_group_ids:
            raise ValueError(f"群配置 YAML 中群号重复：{group_id}")
        if not isinstance(item, dict):
            raise ValueError(f"群配置 YAML 的群 {group_id} 必须是对象")

        group_config = GroupConfig(group_id=group_id)
        _apply_mapping_group_fields(group_config, defaults, base_dir=base_dir)
        _apply_mapping_group_fields(group_config, item, base_dir=base_dir)
        result.append(group_config)
        existing_group_ids.add(group_id)

    return result


class PluginConfig(BaseModel):
    """插件全局配置。"""
    model_config = ConfigDict(extra="allow")
    groups: dict[str, GroupConfig] = Field(default_factory=dict)
    bili_cookie: str = ""
    bili_refresh_token: str = ""
    bili_refresh_pending: dict[str, Any] = Field(default_factory=dict)
    credential_generation: int = 0
    admin_qqs: list[str] = Field(default_factory=list)
    # 异常通知模式：all=通知超管+普通管理员, superusers_only=仅超管, none=不通知任何人
    notify_admins_on_abnormal_request: str = "superusers_only"
    webui_enabled: bool = False
    webui_host: str = "127.0.0.1"
    webui_port: int = 1128
    webui_token: str = ""
    webui_custom_css_file: str = ""
    webui_require_https: bool = False
    webui_trust_proxy_headers: bool = False
    webui_session_timeout_seconds: int = 3600
    webui_login_max_attempts: int = 5
    webui_login_window_seconds: int = 900


# ---------------------------------------------------------------------------
# 配置管理器
# ---------------------------------------------------------------------------

class LegacyConfigLoader:
    """Read legacy JSON, YAML and effective environment without rewriting them."""
    _config: Optional[PluginConfig] = None

    @classmethod
    def load(cls, settings=None) -> PluginConfig:
        """Compile the exact legacy precedence once, with paths rooted at app_root."""
        global CONFIG_ROOT, DATA_PATH, CONFIG_FILE, GROUP_OVERRIDES_FILE, LEGACY_CONFIG_FILE
        settings = settings or get_settings()
        SOURCE_PATHS.clear()
        CONFIG_ROOT = Path(getattr(settings, "app_root", Path.cwd())).resolve()
        DATA_PATH = CONFIG_ROOT / "data/runtime"
        CONFIG_FILE = DATA_PATH / "plugin_state.json"
        GROUP_OVERRIDES_FILE = DATA_PATH / "group_overrides.json"
        LEGACY_CONFIG_FILE = CONFIG_ROOT / "data/config/plugin_config.json"
        cls._load_config(settings)
        assert cls._config is not None
        return cls._config

    @staticmethod
    def _restrict_state_permissions(path: Path) -> None:
        """Keep a persisted Bilibili Cookie readable only by the service account."""
        try:
            path.chmod(0o600)
        except OSError:
            if os.name == "posix":
                raise
            logger.warning("无法限制运行时配置文件权限：%s", path, exc_info=True)

    # ---- 兼容旧版 GROUP_CONFIGS JSON 环境变量 ----

    @classmethod
    def _merge_legacy_env_groups(cls, file_config: PluginConfig, driver_config) -> None:
        """兼容旧的 GROUP_CONFIGS={"群号":{...}} JSON 格式。"""
        env_groups_str = (
            getattr(driver_config, "group_configs", "")
            or os.getenv("GROUP_CONFIGS", "")
        )
        if not env_groups_str:
            return
        try:
            env_groups = json.loads(env_groups_str)
            if isinstance(env_groups, dict):
                for gid, gcfg in env_groups.items():
                    if not isinstance(gcfg, dict):
                        continue
                    merged = dict(gcfg)
                    merged["group_id"] = gid
                    file_config.groups[gid] = GroupConfig(**merged)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    @classmethod
    def _merge_group_config_file(cls, file_config: PluginConfig, driver_config) -> None:
        """读取 GROUP_CONFIG_FILE 指向的群配置 YAML。"""
        group_config_file = (
            getattr(driver_config, "group_config_file", "")
            or os.getenv("GROUP_CONFIG_FILE", "")
        )
        if not group_config_file:
            return

        for group_config in load_group_configs_from_yaml_file(group_config_file):
            file_config.groups[group_config.group_id] = group_config

    @classmethod
    def _refresh_review_stages(cls, file_config: PluginConfig) -> None:
        """Use each group's YAML as the source of truth after config merges."""
        for group in file_config.groups.values():
            if not group.review_pipeline_file.strip():
                continue
            pipeline_file = resolve_config_path(group.review_pipeline_file).resolve()
            group.review_pipeline_file = str(pipeline_file)
            group.review_stages = load_review_stages_from_yaml_file(pipeline_file)

    @classmethod
    def _merge_runtime_group_overrides(cls, file_config: PluginConfig) -> None:
        """应用管理命令保存的群配置覆盖项，避免重启后被基础 YAML 覆盖。"""
        if not GROUP_OVERRIDES_FILE.exists():
            return
        try:
            payload = json.loads(GROUP_OVERRIDES_FILE.read_text(encoding="utf-8"))
            groups = payload.get("groups", {})
            if not isinstance(groups, dict):
                raise ValueError("groups 必须是对象")
            for group_id, fields in groups.items():
                if not isinstance(fields, dict):
                    raise ValueError(f"群 {group_id} 的覆盖配置必须是对象")
                group_config = file_config.groups.get(str(group_id)) or GroupConfig(
                    group_id=str(group_id)
                )
                _apply_mapping_group_fields(group_config, fields)
                file_config.groups[str(group_id)] = group_config
        except Exception:
            logger.exception("加载群配置运行时覆盖文件失败，已忽略该文件")

    # ---- 新格式：逐项环境变量 GROUP_<字段名> ----

    @classmethod
    def _apply_env_group_fields(cls, group_config: GroupConfig, get_env, prefix: str = "GROUP_") -> None:
        """按统一字段规则将环境变量写入单个群配置。"""
        enabled = get_env(f"{prefix}ENABLED")
        if enabled:
            group_config.enabled = parse_bool(enabled)

        required_qq_level = get_env(f"{prefix}REQUIRED_QQ_LEVEL")
        if required_qq_level:
            try:
                group_config.required_qq_level = int(required_qq_level)
            except (ValueError, TypeError):
                pass

        required_bili_level = get_env(f"{prefix}REQUIRED_BILI_LEVEL")
        if required_bili_level:
            try:
                group_config.required_bili_level = int(required_bili_level)
            except (ValueError, TypeError):
                pass

        target_uids = get_env(f"{prefix}TARGET_UIDS")
        if target_uids:
            group_config.target_uids = parse_uid_list(target_uids)

        target_medal_uids = get_env(f"{prefix}TARGET_MEDAL_UIDS")
        if target_medal_uids:
            group_config.target_medal_uids = parse_uid_list(target_medal_uids)

        required_medal_level = get_env(f"{prefix}REQUIRED_MEDAL_LEVEL")
        if required_medal_level:
            try:
                group_config.required_medal_level = int(required_medal_level)
            except (ValueError, TypeError):
                pass

        reject_reason_with_leave_time = get_env(f"{prefix}REJECT_REASON_WITH_LEAVE_TIME")
        if reject_reason_with_leave_time:
            group_config.reject_reason_with_leave_time = parse_bool(
                reject_reason_with_leave_time
            )

        custom_reject_uids = get_env(f"{prefix}CUSTOM_REJECT_UIDS")
        if custom_reject_uids:
            try:
                group_config.custom_reject_uids = parse_custom_reject_uids(
                    custom_reject_uids
                )
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        reject_reasons = get_env(f"{prefix}REJECT_REASONS")
        if reject_reasons:
            try:
                group_config.reject_reasons = parse_reject_reasons(reject_reasons)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        review_pipeline_file = get_env(f"{prefix}REVIEW_PIPELINE_FILE")
        if review_pipeline_file:
            resolved_pipeline_file = resolve_config_path(review_pipeline_file).resolve()
            group_config.review_pipeline_file = str(resolved_pipeline_file)
            group_config.review_stages = load_review_stages_from_yaml_file(
                resolved_pipeline_file
            )

    @classmethod
    def _sanitize_runtime_payload(cls, payload: Any) -> tuple[dict[str, Any], list[str]]:
        """尽量兼容旧版运行时 JSON，避免因审核分组格式升级而丢失整份配置。"""
        if not isinstance(payload, dict):
            raise ValueError("运行时配置根节点必须是对象")

        sanitized = dict(payload)
        warnings: list[str] = []
        groups = sanitized.get("groups")
        if not isinstance(groups, dict):
            return sanitized, warnings

        sanitized_groups: dict[str, Any] = {}
        for group_id, raw_group in groups.items():
            if not isinstance(raw_group, dict):
                sanitized_groups[group_id] = raw_group
                continue
            normalized_group = dict(raw_group)
            review_stages = normalized_group.get("review_stages")
            if isinstance(review_stages, list):
                invalid_legacy_stage = any(
                    isinstance(stage, dict)
                    and (
                        "conditions" not in stage
                        or "rule" in stage
                        or "rule_expression" in stage
                    )
                    for stage in review_stages
                )
                if invalid_legacy_stage:
                    normalized_group.pop("review_stages", None)
                    warnings.append(
                        f"群 {group_id} 的运行时 review_stages 为旧版格式，已忽略并保留其余配置"
                    )
            sanitized_groups[group_id] = normalized_group
        sanitized["groups"] = sanitized_groups
        return sanitized, warnings

    @classmethod
    def _load_runtime_config_file(cls, path: Path) -> PluginConfig:
        """加载运行时配置文件，并对旧版结构做最小迁移。"""
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
        sanitized_payload, warnings = cls._sanitize_runtime_payload(payload)
        for message in warnings:
            logger.warning(message)
        return PluginConfig.model_validate(sanitized_payload)

    @classmethod
    def _merge_single_env_group(cls, file_config: PluginConfig, driver_config) -> None:
        """读取 GROUP_ID / GROUP_ENABLED 等独立环境变量，合并到一个或多个群。"""
        group_ids = (
            getattr(driver_config, "group_id", "")
            or os.getenv("GROUP_ID", "")
        )
        if not group_ids:
            return

        def get_env(name: str, default: str = "") -> str:
            """从 driver config 或系统环境变量中取值。"""
            return getattr(driver_config, name.lower(), "") or os.getenv(name, default)

        for group_id in parse_str_list(group_ids):
            group_config = file_config.groups.get(group_id) or GroupConfig(group_id=group_id)
            cls._apply_env_group_fields(group_config, get_env)
            file_config.groups[group_id] = group_config

    # ---- 新格式：编号多群环境变量 GROUP_N_<字段名> ----

    @classmethod
    def _merge_numbered_env_groups(cls, file_config: PluginConfig, driver_config) -> None:
        """
        扫描 GROUP_1_ID、GROUP_2_ID 等编号格式的环境变量，一次读入多个群的配置。

        用法示例 (.env.prod):
          GROUP_1_ID=123456789
          GROUP_1_ENABLED=true
          GROUP_1_CHECK_FOLLOW=true
          GROUP_1_TARGET_UIDS=主播UID
          GROUP_1_CHECK_MEDAL=true
          GROUP_1_TARGET_MEDAL_UIDS=主播UID
          GROUP_1_REQUIRED_MEDAL_LEVEL=10
          …

          GROUP_2_ID=123456789
          GROUP_2_ENABLED=false
          …

        编号从 1 开始，遇到第一个不存在的编号即停止扫描。
        """
        def get_env(name: str, default: str = "") -> str:
            return getattr(driver_config, name.lower(), "") or os.getenv(name, default)

        index = 1
        while True:
            gid = get_env(f"GROUP_{index}_ID")
            if not gid:
                break
            gid = str(gid)

            group_config = file_config.groups.get(gid) or GroupConfig(group_id=gid)
            cls._apply_env_group_fields(group_config, get_env, prefix=f"GROUP_{index}_")
            file_config.groups[gid] = group_config
            index += 1

    # ---- 公共 API ----

    @classmethod
    def get_config(cls) -> PluginConfig:
        """获取全局配置（首次调用时从文件和环境变量加载）。"""
        if cls._config is None:
            cls._load_config()
        return cls._config or PluginConfig()

    @classmethod
    def _load_config(cls, settings=None) -> None:
        """加载配置：先读运行时状态，再合并环境变量。"""
        driver_config = settings or get_settings()
        file_config: Optional[PluginConfig] = None

        # 优先从新的运行时状态文件读取；若不存在则兼容旧路径
        if CONFIG_FILE.exists():
            cls._restrict_state_permissions(CONFIG_FILE)
            try:
                file_config = cls._load_runtime_config_file(CONFIG_FILE)
            except Exception as exc:
                raise ValueError(f"旧配置无法读取，已停止首次迁移：{CONFIG_FILE}") from exc
        elif LEGACY_CONFIG_FILE.exists():
            cls._restrict_state_permissions(LEGACY_CONFIG_FILE)
            try:
                file_config = cls._load_runtime_config_file(LEGACY_CONFIG_FILE)
            except Exception as exc:
                raise ValueError(f"旧配置无法读取，已停止首次迁移：{LEGACY_CONFIG_FILE}") from exc
        else:
            file_config = PluginConfig()

        # 合并 .env.prod 中的 BILI_COOKIE（环境变量值覆盖文件值）
        env_cookie = (
            getattr(driver_config, "bili_cookie", "")
            or os.getenv("BILI_COOKIE", "")
        )
        if env_cookie:
            file_config.bili_cookie = env_cookie

        env_admin_qqs = (
            getattr(driver_config, "admin_qqs", "")
            or os.getenv("ADMIN_QQS", "")
        )
        if env_admin_qqs:
            file_config.admin_qqs = parse_str_list(env_admin_qqs)

        env_notify_admins_on_abnormal_request = (
            getattr(driver_config, "notify_admins_on_abnormal_request", "")
            or os.getenv("NOTIFY_ADMINS_ON_ABNORMAL_REQUEST", "")
        )
        if env_notify_admins_on_abnormal_request:
            raw = str(env_notify_admins_on_abnormal_request).strip().lower()
            # 兼容旧版 true/false 及新版 all/superusers_only/none
            if raw in BOOL_TRUE_VALUES:
                file_config.notify_admins_on_abnormal_request = "all"
            elif raw in ("false", "superusers_only", "superusers"):
                file_config.notify_admins_on_abnormal_request = "superusers_only"
            elif raw == "none":
                file_config.notify_admins_on_abnormal_request = "none"
            elif raw == "all":
                file_config.notify_admins_on_abnormal_request = "all"

        def env_value(name: str, default: Any = "") -> Any:
            return getattr(driver_config, name.lower(), "") or os.getenv(name, default)

        bool_fields = {
            "webui_enabled": "WEBUI_ENABLED",
            "webui_require_https": "WEBUI_REQUIRE_HTTPS",
            "webui_trust_proxy_headers": "WEBUI_TRUST_PROXY_HEADERS",
        }
        for field_name, env_name in bool_fields.items():
            raw_value = env_value(env_name)
            if raw_value not in ("", None):
                setattr(file_config, field_name, parse_bool(raw_value))

        string_fields = {
            "webui_host": "WEBUI_HOST",
            "webui_token": "WEBUI_TOKEN",
            "webui_custom_css_file": "WEBUI_CUSTOM_CSS_FILE",
        }
        for field_name, env_name in string_fields.items():
            raw_value = env_value(env_name)
            if raw_value not in ("", None):
                setattr(file_config, field_name, str(raw_value).strip())

        int_fields = {
            "webui_port": ("WEBUI_PORT", 1, 65535),
            "webui_session_timeout_seconds": ("WEBUI_SESSION_TIMEOUT_SECONDS", 300, 86400),
            "webui_login_max_attempts": ("WEBUI_LOGIN_MAX_ATTEMPTS", 1, 20),
            "webui_login_window_seconds": ("WEBUI_LOGIN_WINDOW_SECONDS", 60, 3600),
        }
        for field_name, (env_name, minimum, maximum) in int_fields.items():
            raw_value = env_value(env_name)
            if raw_value in ("", None):
                continue
            try:
                parsed = int(raw_value)
            except (TypeError, ValueError):
                logger.warning("忽略无效配置 %s=%r", env_name, raw_value)
                continue
            setattr(file_config, field_name, max(minimum, min(maximum, parsed)))

        # 推荐的结构化群配置 YAML
        cls._merge_group_config_file(file_config, driver_config)

        # 管理命令修改项覆盖基础 YAML；后续显式 GROUP_* 环境变量仍拥有最高优先级
        cls._merge_runtime_group_overrides(file_config)

        # 兼容旧的 GROUP_CONFIGS JSON 配置
        cls._merge_legacy_env_groups(file_config, driver_config)

        # 支持更易读的单组环境变量格式
        cls._merge_single_env_group(file_config, driver_config)

        # 支持编号格式的多群环境变量 (GROUP_1_ID / GROUP_2_ID ...)
        cls._merge_numbered_env_groups(file_config, driver_config)

        # 运行时 JSON 中的 review_stages 可能是旧快照；以 YAML 当前内容为准。
        cls._refresh_review_stages(file_config)

        cls._config = file_config
