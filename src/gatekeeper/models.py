"""Framework independent data exchanged by review and adapter code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CheckResult:
    """单项校验结果。"""

    state: str
    detail: str = ""
    matched_target_uid: int | None = None
    matched_level: int | None = None


@dataclass
class RuleConditionStatus:
    """单个规则条件的求值结果。"""

    state: str
    reason: str = ""
    # Raw measurements are distinct from the legacy threshold result. Keeping
    # them out of equality preserves the old public T/F/I value contract.
    observation: int | float | None = field(default=None, compare=False)


@dataclass
class StageRuntime:
    """编译后的单个审核阶段。"""

    index: int
    name: str
    conditions: list[str]
    mode: str
    on_true: list[str]
    on_false: list[str]
    on_unknown: list[str]
    on_true_target: int | None = None
    on_false_target: int | None = None
    on_unknown_target: int | None = None
    on_true_reason: str = ""
    on_false_reason: str = ""
    on_unknown_reason: str = ""
    when: str = ""


@dataclass
class StageDispatchResult:
    """阶段动作执行后的结果。"""

    flow_action: str
    target_stage_index: int | None = None


@dataclass
class StageExecutionResult:
    """单个或整条审核管线执行后的结果。"""

    stage: StageRuntime
    outcome: str
    actions: list[str]
    reasons: list[str]
    dispatch_result: StageDispatchResult


@dataclass
class PipelineStep:
    """One recorded stage, with its planned flow and optional side effects."""

    execution: StageExecutionResult
    trace: str
    dispatch_actions: bool = True


class LeaveRecordLike(Protocol):
    """The only leave record detail needed to evaluate a review condition."""

    leave_time: str
