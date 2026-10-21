"""Small, bounded AST evaluator with Kleene three-valued boolean semantics.

No Python evaluation is performed. Numeric comparisons read observations while
bare condition names retain the v2 configured-threshold T/F/I meaning.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from .config import REVIEW_CONDITIONS
from .models import RuleConditionStatus

NUMERIC_CONDITIONS = {"qq_level": "qq_level", "bili_level": "bili_level", "medal": "medal", "medal_level": "medal"}
DISPLAY_NAMES = {
    "qq_level": "QQ等级", "bili_level": "B站等级", "medal_level": "粉丝牌等级",
    "follow": "关注", "medal": "粉丝牌", "bili_account": "B站账号确认",
    "no_leave_record": "无退群记录", "no_conflict": "B站账号归属校验",
    "no_identity_change": "不更换B站账号",
}
STATES = {"true", "false", "unknown"}
MAX_EXPRESSION_LENGTH = 2048
MAX_AST_NODES = 128
MAX_AST_DEPTH = 24


def kleene_not(value: str) -> str:
    return {"true": "false", "false": "true"}.get(value, "unknown")


def kleene_and(values: list[str]) -> str:
    return "false" if "false" in values else "unknown" if "unknown" in values else "true"


def kleene_or(values: list[str]) -> str:
    return "true" if "true" in values else "unknown" if "unknown" in values else "false"


def normalize_expression(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("审核表达式不能为空")
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ValueError("审核表达式过长")
    return re.sub(r"!(?!=)", " not ", source.replace("&&", " and ").replace("||", " or ")).strip()


def _literal_number(node: ast.AST) -> int | float | None:
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        value = _literal_number(node.operand)
        return -value if value is not None else None
    return None


def _validate(tree: ast.Expression) -> tuple[str, ...]:
    conditions: dict[str, None] = {}
    count = 0

    def visit(node: ast.AST, depth: int, *, numeric: bool = False) -> None:
        nonlocal count
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise ValueError("审核表达式过于复杂")
        if numeric:
            if isinstance(node, ast.Name) and node.id in NUMERIC_CONDITIONS:
                conditions[NUMERIC_CONDITIONS[node.id]] = None
                return
            value = _literal_number(node)
            if value is not None and abs(value) <= 10**12 and math.isfinite(value):
                return
            raise ValueError("比较只允许原始等级数值和有限数字常量")
        if isinstance(node, ast.Name) and node.id in REVIEW_CONDITIONS:
            conditions[node.id] = None
        elif isinstance(node, ast.Constant) and (type(node.value) is bool or node.value is None):
            return
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            for value_node in node.values:
                visit(value_node, depth + 1)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            visit(node.operand, depth + 1)
        elif isinstance(node, ast.Compare) and all(
            isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops
        ):
            for value_node in [node.left, *node.comparators]:
                visit(value_node, depth + 1, numeric=True)
        else:
            raise ValueError(f"审核表达式不允许语法：{type(node).__name__}")

    visit(tree.body, 1)
    return tuple(conditions)


def _numeric_value(node: ast.AST, statuses: dict[str, RuleConditionStatus]) -> int | float | None:
    if isinstance(node, ast.Name):
        status = statuses.get(NUMERIC_CONDITIONS[node.id])
        value = status.observation if status is not None else None
    else:
        value = _literal_number(node)
    if type(value) not in {int, float} or abs(value) > 10**12 or not math.isfinite(value):
        return None
    return value


def _evaluate(node: ast.AST, statuses: dict[str, RuleConditionStatus]) -> str:
    if isinstance(node, ast.Name):
        status = statuses.get(node.id)
        return status.state if status is not None and status.state in STATES else "unknown"
    if isinstance(node, ast.Constant):
        return "unknown" if node.value is None else "true" if node.value else "false"
    if isinstance(node, ast.UnaryOp):
        return kleene_not(_evaluate(node.operand, statuses))
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(child, statuses) for child in node.values]
        return kleene_and(values) if isinstance(node.op, ast.And) else kleene_or(values)
    if isinstance(node, ast.Compare):
        values = [_numeric_value(value, statuses) for value in [node.left, *node.comparators]]
        results = []
        for left, op, right in zip(values, node.ops, values[1:]):
            if left is None or right is None:
                results.append("unknown")
                continue
            if isinstance(op, ast.Eq):
                passed = left == right
            elif isinstance(op, ast.NotEq):
                passed = left != right
            elif isinstance(op, ast.Lt):
                passed = left < right
            elif isinstance(op, ast.LtE):
                passed = left <= right
            elif isinstance(op, ast.Gt):
                passed = left > right
            else:
                passed = left >= right
            results.append("true" if passed else "false")
        return kleene_and(results)
    raise ValueError("未校验的表达式节点")


def _causes(node: ast.AST, statuses: dict[str, RuleConditionStatus]) -> list[str]:
    state = _evaluate(node, statuses)
    if isinstance(node, ast.BoolOp):
        selected = node.values
        if (isinstance(node.op, ast.And) and state == "false") or (
            isinstance(node.op, ast.Or) and state == "true"
        ) or state == "unknown":
            selected = [child for child in node.values if _evaluate(child, statuses) == state]
        return [reason for child in selected for reason in _causes(child, statuses)]
    if isinstance(node, ast.UnaryOp):
        # Describe the observed child, not an inverted fictitious failed check.
        return _causes(node.operand, statuses)
    if isinstance(node, ast.Name):
        name = DISPLAY_NAMES.get(node.id, node.id)
        status = statuses.get(node.id)
        if state == "true":
            return [f"{name}条件已满足"]
        if status is not None and status.reason.strip():
            return [status.reason.strip()]
        return [f"{name}条件{'未满足' if state == 'false' else '暂无法核验'}"]
    if isinstance(node, ast.Compare):
        observations = []
        for child in [node.left, *node.comparators]:
            if isinstance(child, ast.Name):
                value = _numeric_value(child, statuses)
                name = "粉丝牌等级" if child.id == "medal" else DISPLAY_NAMES[child.id]
                observations.append(f"{name}当前{value}" if value is not None else f"暂无法读取{name}")
        comparison = ast.unparse(node)
        result = {"true": "满足", "false": "未满足", "unknown": "无法核验"}[state]
        return [f"{'、'.join(observations) or '数值比较'}，{result} {comparison}"]
    return [f"常量规则为{'无法判断' if state == 'unknown' else state}"]


@dataclass(frozen=True)
class CompiledExpression:
    source: str
    conditions: tuple[str, ...]
    _tree: ast.Expression

    def evaluate(self, statuses: dict[str, RuleConditionStatus]) -> str:
        return _evaluate(self._tree.body, statuses)

    def explain(self, statuses: dict[str, RuleConditionStatus]) -> str:
        outcome = self.evaluate(statuses)
        result = {"true": "已满足", "false": "未满足", "unknown": "暂无法确认"}[outcome]
        details = "；".join(dict.fromkeys(_causes(self._tree.body, statuses)))
        action = "请稍后重试或联系管理员" if outcome == "unknown" else "请核对本群规则，如需复核请联系管理员"
        return f"审核表达式【{self.source}】{result}：{details}；{action}"


@lru_cache(maxsize=128)
def compile_expression(source: str) -> CompiledExpression:
    normalized = normalize_expression(source)
    try:
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("审核表达式语法错误") from exc
    conditions = _validate(tree)
    return CompiledExpression(source=source.strip(), conditions=conditions, _tree=tree)


def evaluate_expression(source: str, statuses: dict[str, RuleConditionStatus]) -> str:
    return compile_expression(source).evaluate(statuses)
