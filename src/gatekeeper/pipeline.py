"""Lazy, deterministic execution of review groups and their T/F/I routing."""

from collections.abc import Generator

from .config import GroupConfig
from .formatters import format_stage_trace
from .models import (
    PipelineStep,
    RuleConditionStatus,
    StageDispatchResult,
    StageExecutionResult,
    StageRuntime,
)


def evaluate_stage_conditions(stage: StageRuntime, condition_statuses: dict[str, RuleConditionStatus]) -> str:
    """按分组模式汇总 T/F/I，不解析任何布尔表达式。"""
    states = [condition_statuses[name].state for name in stage.conditions]
    if stage.mode == "all_pass":
        if "false" in states:
            return "false"
        if "unknown" in states:
            return "unknown"
        return "true"
    if "true" in states:
        return "true"
    if "unknown" in states:
        return "unknown"
    return "false"


def build_stage_runtime(
    *,
    index: int,
    name: str,
    conditions: list[str],
    mode: str,
    on_true: list[str],
    on_false: list[str],
    on_unknown: list[str],
    on_true_target: int | None = None,
    on_false_target: int | None = None,
    on_unknown_target: int | None = None,
    on_true_reason: str = "",
    on_false_reason: str = "",
    on_unknown_reason: str = "",
) -> StageRuntime:
    """编译单个审核阶段。"""
    return StageRuntime(
        index=index,
        name=name or f"阶段{index}",
        conditions=list(conditions),
        mode=mode,
        on_true=on_true,
        on_false=on_false,
        on_unknown=on_unknown,
        on_true_target=on_true_target,
        on_false_target=on_false_target,
        on_unknown_target=on_unknown_target,
        on_true_reason=on_true_reason,
        on_false_reason=on_false_reason,
        on_unknown_reason=on_unknown_reason,
    )


def validate_stage_routing(stages: list[StageRuntime]) -> None:
    """校验分组编号与跳转目标是否合法。"""
    indexes = [stage.index for stage in stages]
    if len(indexes) != len(set(indexes)):
        raise ValueError("审核分组编号不能重复")

    existing_indexes = set(indexes)
    for stage in stages:
        for branch_name, target in (
            ("on_true", stage.on_true_target),
            ("on_false", stage.on_false_target),
            ("on_unknown", stage.on_unknown_target),
        ):
            if target is None:
                continue
            if target == stage.index:
                raise ValueError(f"阶段 {stage.index} 的 {branch_name}_target 不能指向自己")
            if target not in existing_indexes:
                raise ValueError(f"阶段 {stage.index} 的 {branch_name}_target 指向不存在的分组 {target}")


def get_review_stages(group_config: GroupConfig) -> list[StageRuntime]:
    """获取当前群的审核分组；审核逻辑必须由分组管道提供。"""
    if not group_config.review_stages:
        raise ValueError("未配置审核分组管道 review_pipeline_file")
    runtimes = [
        build_stage_runtime(
            index=stage.index,
            name=stage.name,
            conditions=stage.conditions,
            mode=stage.mode,
            on_true=stage.on_true,
            on_false=stage.on_false,
            on_unknown=stage.on_unknown,
            on_true_target=stage.on_true_target,
            on_false_target=stage.on_false_target,
            on_unknown_target=stage.on_unknown_target,
            on_true_reason=stage.on_true_reason,
            on_false_reason=stage.on_false_reason,
            on_unknown_reason=stage.on_unknown_reason,
        )
        for stage in sorted(group_config.review_stages, key=lambda item: item.index)
    ]
    validate_stage_routing(runtimes)
    return runtimes


def collect_pipeline_conditions(stages: list[StageRuntime]) -> set[str]:
    """收集整个审核管线中用到的条件。"""
    conditions: set[str] = set()
    for stage in stages:
        conditions.update(stage.conditions)
    return conditions


def get_stage_outcome_actions(stage: StageRuntime, outcome: str) -> list[str]:
    """获取阶段某个结果对应的动作列表。"""
    if outcome == "true":
        return stage.on_true
    if outcome == "false":
        return stage.on_false
    return stage.on_unknown


def get_stage_outcome_target(stage: StageRuntime, outcome: str) -> int | None:
    """获取阶段某个结果对应的跳转目标。"""
    if outcome == "true":
        return stage.on_true_target
    if outcome == "false":
        return stage.on_false_target
    return stage.on_unknown_target


def get_stage_flow_action(actions: list[str]) -> str:
    """从阶段动作列表中解析流转动作。"""
    for action in actions:
        if action in {"next", "approve", "reject", "ignore"}:
            return action
    raise ValueError("阶段动作缺少流转动作")


def get_next_stage_index(stages: list[StageRuntime], current_stage_index: int) -> int | None:
    """获取顺序模式下的下一个分组编号。"""
    for stage in stages:
        if stage.index > current_stage_index:
            return stage.index
    return None


def resolve_stage_reasons(
    stage: StageRuntime,
    outcome: str,
    referenced_statuses: list[RuleConditionStatus],
) -> list[str]:
    """生成阶段结果对应的原因列表。"""
    override_reason = {
        "true": stage.on_true_reason,
        "false": stage.on_false_reason,
        "unknown": stage.on_unknown_reason,
    }.get(outcome, "")
    if override_reason:
        return [override_reason]

    if outcome == "false":
        reasons = [status.reason for status in referenced_statuses if status.state == "false" and status.reason]
        return reasons or ["未满足阶段规则"]

    if outcome == "unknown":
        reasons = [status.reason for status in referenced_statuses if status.state == "unknown" and status.reason]
        return reasons or ["阶段所需条件暂时无法完整判断"]

    return [f"通过阶段：{stage.name}"]


def iter_pipeline(
    stages: list[StageRuntime],
    condition_statuses: dict[str, RuleConditionStatus],
) -> Generator[PipelineStep, StageDispatchResult | None, StageExecutionResult]:
    """Yield traces before dispatch, accepting an adapter's executed flow via send.

    Resuming with ``None`` follows the planned flow; passing a dispatch result
    preserves action handlers which override that flow. Successful fall-through
    yields one final approve step with ``dispatch_actions=False``.
    """
    if not stages:
        raise ValueError("审核分组不能为空")

    sorted_stages = sorted(stages, key=lambda item: item.index)
    stage_map = {stage.index: stage for stage in sorted_stages}
    current_stage_index = sorted_stages[0].index
    visited_indexes: set[int] = set()

    while True:
        if current_stage_index in visited_indexes:
            raise ValueError(f"审核分组跳转形成循环，重复进入分组 {current_stage_index}")
        visited_indexes.add(current_stage_index)

        current_stage = stage_map.get(current_stage_index)
        if current_stage is None:
            raise ValueError(f"审核分组跳转到不存在的分组 {current_stage_index}")

        referenced_statuses = [
            condition_statuses[name] for name in current_stage.conditions if name in condition_statuses
        ]
        outcome = evaluate_stage_conditions(current_stage, condition_statuses)
        reasons = resolve_stage_reasons(current_stage, outcome, referenced_statuses)
        actions = get_stage_outcome_actions(current_stage, outcome)
        # The original dispatcher validated flows after recording their trace.
        planned_flow = next(
            (action for action in actions if action in {"next", "approve", "reject", "ignore"}),
            "",
        )
        execution = StageExecutionResult(
            stage=current_stage,
            outcome=outcome,
            actions=actions,
            reasons=reasons,
            dispatch_result=StageDispatchResult(
                flow_action=planned_flow,
                target_stage_index=get_stage_outcome_target(current_stage, outcome),
            ),
        )
        dispatched = yield PipelineStep(
            execution=execution,
            trace=format_stage_trace(current_stage, outcome, actions, reasons),
        )
        if not planned_flow:
            get_stage_flow_action(actions)
        if dispatched is not None:
            execution.dispatch_result = dispatched
        if execution.dispatch_result.flow_action != "next":
            return execution

        next_stage_index = execution.dispatch_result.target_stage_index
        if next_stage_index is None:
            next_stage_index = get_next_stage_index(sorted_stages, current_stage.index)

        if next_stage_index is None:
            if execution.outcome != "true":
                raise ValueError(
                    f"阶段 {current_stage.index} 的 {execution.outcome} 分支配置为 next，但不存在下一个分组"
                )
            fallback_reasons = ["全部审核分组均已放行"]
            fallback_actions = ["approve"]
            fallback = StageExecutionResult(
                stage=current_stage,
                outcome="true",
                actions=fallback_actions,
                reasons=fallback_reasons,
                dispatch_result=StageDispatchResult(flow_action="approve"),
            )
            yield PipelineStep(
                execution=fallback,
                trace=format_stage_trace(current_stage, "true", fallback_actions, fallback_reasons),
                dispatch_actions=False,
            )
            return fallback

        current_stage_index = next_stage_index
