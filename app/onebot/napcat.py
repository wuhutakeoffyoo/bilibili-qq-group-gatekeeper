"""Conservative request-specific evidence from NapCat's system-message API.

Verified schema: NapNeko/NapCatQQ @ 450fb0429a064e3418e0c698b1b4d78324101929,
packages/napcat-onebot/action/system/GetSystemMsg.ts. A matching explicitly
unchecked request proves only that it is still unhandled. ``checked=True``
doesn't identify the executed action and is never proof of approval/rejection.
"""
from __future__ import annotations

import asyncio

from websockets.exceptions import ConnectionClosed

from app.gatekeeper.decisions import RequestEvidence
from app.onebot.client import OneBotDisconnected

MAX_SAFE_INTEGER = 2 ** 53 - 1


def _decimal(value):
    if type(value) is int and 0 < value <= MAX_SAFE_INTEGER:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
        if 0 < number <= MAX_SAFE_INTEGER and str(number) == value:
            return value
    return None


class NapCatRequestInspector:
    def __init__(self, get_client):
        self.get_client = get_client

    async def __call__(self, metadata):
        fields = {key: str(metadata.get(key, "")) for key in
                  ("bot_id", "group_id", "qq", "sub_type", "flag", "expected_result")}

        def unknown(detail):
            return RequestEvidence(state="unknown", detail=detail, **fields)

        if fields["sub_type"] != "add" or fields["expected_result"] not in {"approved", "rejected"}:
            return unknown("该请求类型不支持精确自动对账")
        flag = _decimal(fields["flag"])
        if flag is None:
            return unknown("申请标识不是可无损核对的NapCat数字序号，请人工确认")
        client = self.get_client(fields["bot_id"])
        if client is None:
            return RequestEvidence(state="unavailable", detail="原审批机器人当前未连接，稍后重试只读对账", **fields)
        if str(client.self_id) != fields["bot_id"]:
            return unknown("核验连接与原审批机器人不匹配")
        try:
            version = await client.call_action("get_version_info")
            if not isinstance(version, dict) or "napcat" not in str(version.get("app_name", "")).casefold():
                return unknown("当前OneBot实现未确认支持NapCat请求级核验")
            payload = await client.call_action("get_group_system_msg", count=100)
        except (asyncio.TimeoutError, OneBotDisconnected, ConnectionClosed, OSError) as exc:
            return RequestEvidence(state="unavailable", detail=f"请求级核验暂时无法连接（{type(exc).__name__}）", **fields)
        except Exception as exc:
            return unknown(f"请求级核验接口不可用（{type(exc).__name__}），请人工确认")
        rows = payload.get("join_requests") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return unknown("申请列表响应结构不完整，请人工确认")
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # NapCat coerces seq to a JS Number; larger IDs can collide after
            # rounding. Accept only an exact safe JSON integer from this field.
            request_id = row.get("request_id")
            if type(request_id) is not int or not (0 < request_id <= MAX_SAFE_INTEGER):
                continue
            if (str(request_id) == flag and _decimal(row.get("group_id")) == fields["group_id"]
                    and _decimal(row.get("invitor_uin")) == fields["qq"]):
                matches.append(row)
        if len(matches) != 1:
            return unknown("未找到唯一匹配原QQ、群号及申请标识的记录，请人工确认")
        if matches[0].get("checked") is not False:
            return unknown("该申请已被处理或状态不明，无法确认同意或拒绝的实际结果，请人工确认")
        return RequestEvidence(state="unhandled", detail="NapCat精确匹配的该条申请仍明确未处理", **fields)
