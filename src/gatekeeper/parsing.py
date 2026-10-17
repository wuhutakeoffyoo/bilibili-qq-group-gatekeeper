"""Parse incoming application answers and account metadata without I/O."""

import re


def extract_bili_name(raw_comment: str) -> str:
    """从加群申请 comment 中提取 B站昵称。

    QQ 群如果开启了加群验证问题，NapCat 传回的 comment 格式为：
      问题：自己b站id+主播名称
      答案：Nyako_WW
    此时只取"答案："后面的部分；如果没有 Q&A 格式，直接用原值。
    """
    text = raw_comment.strip()
    if not text:
        return ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        for prefix in ("答案：", "答案:", "answer：", "answer:"):
            if line.startswith(prefix):
                return line.removeprefix(prefix).strip()
    # 无 Q&A 格式 → 直接返回原值
    return text


def parse_qq_level(raw_value: object) -> int | None:
    """从 NapCat 返回值中提取 QQ 等级数字。

    不同 OneBot 实现返回的 level 字段格式可能不同：
    - 纯数字 int（如 32）
    - 带文字前缀的字符串（如 "Lv32"、"等级32"）
    这里统一用正则提取第一个连续数字作为等级值。
    字段明确返回 0 时代表有效的 0 级；字段缺失、空值或负数才视为不可读取。
    """
    if raw_value in ("", None):
        return None
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value if raw_value >= 0 else None
    match = re.search(r"-?\d+", str(raw_value))
    if not match:
        return None
    level = int(match.group(0))
    return level if level >= 0 else None
