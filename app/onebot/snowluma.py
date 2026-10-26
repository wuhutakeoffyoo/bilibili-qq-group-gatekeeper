"""SnowLuma 2.2.0 request contract, verified against upstream tag v2.2.0.

Source: protocol/src/qq-info.ts and onebot/src/modules/contact-actions.ts.
The action flag is opaque outside this adapter and must be sent unchanged.
Only event_type=1 proves a join application; 2/22 are invitations.
"""
from __future__ import annotations

import asyncio
from collections import Counter

from websockets.exceptions import ConnectionClosed

from app.gatekeeper.decisions import RequestEvidence
from app.onebot.client import OneBotDisconnected
from app.onebot.napcat import MAX_SAFE_INTEGER, _decimal
from src.gatekeeper.decisions import DecisionRequest


def add_request_identity(flag, group_id):
    if not isinstance(flag, str):
        return None
    parts = flag.split(":")
    if (len(parts) != 6 or parts[:2] != ["slreq", "1"] or parts[4] != "1"
            or parts[5] not in {"0", "1"}):
        return None
    sequence, group = _decimal(parts[2]), _decimal(parts[3])
    return (sequence, group) if sequence and group == str(group_id) else None


def request_identity_keys(request):
    """Alias the same application across NapCat and SnowLuma without editing history.

    The numeric server sequence, bot, group and applicant must all match. Two
    SnowLuma inbox flags for the same join share a lock and audit lookup too.
    """
    identity = add_request_identity(request.flag, request.group_id)
    if request.sub_type != "add" or identity is None:
        return (request.request_key,)
    sequence, group = identity
    return tuple(dict.fromkeys([request.request_key, *[
        DecisionRequest(request.bot_id, group, "add", f"slreq:1:{sequence}:{group}:1:{filtered}").request_key
        for filtered in ("0", "1")],
        DecisionRequest(request.bot_id, group, "add", sequence).request_key]))


def request_rows(payload):
    if not isinstance(payload, list):
        raise ValueError("SnowLuma group system message array is missing")
    return payload


def unhandled_events(payload, bot_id):
    candidates, identities = [], Counter()
    for row in request_rows(payload):
        if not isinstance(row, dict):
            continue
        seq, group = row.get("request_id"), _decimal(row.get("group_id"))
        if type(seq) is not int or not 0 < seq <= MAX_SAFE_INTEGER or group is None:
            continue
        identity = (str(seq), group)
        identities[identity] += 1
        qq, flag = _decimal(row.get("requester_uin")), row.get("flag")
        if (qq is None or add_request_identity(flag, group) != identity
                or row.get("checked") is not False or not isinstance(row.get("message"), str)):
            continue
        candidates.append((identity, {"post_type": "request", "request_type": "group", "sub_type": "add",
            "self_id": int(bot_id), "group_id": int(group), "user_id": int(qq),
            "flag": flag, "comment": row["message"]}))
    return [event for identity, event in candidates if identities[identity] == 1]


class SnowLumaRequestInspector:
    def __init__(self, get_client):
        self.get_client = get_client

    async def __call__(self, metadata):
        fields = {key: str(metadata.get(key, "")) for key in
                  ("bot_id", "group_id", "qq", "sub_type", "flag", "expected_result")}

        def evidence(state, detail):
            return RequestEvidence(state=state, detail=detail, **fields)

        identity = add_request_identity(fields["flag"], fields["group_id"])
        if fields["sub_type"] != "add" or identity is None or fields["expected_result"] not in {"approved", "rejected"}:
            return evidence("unknown", "原申请不是可精确核对的SnowLuma入群标识；旧适配器待确认动作请人工核对")
        client = self.get_client(fields["bot_id"])
        if client is None:
            return evidence("unavailable", "原审批机器人当前未连接，稍后重试只读对账")
        if str(client.self_id) != fields["bot_id"]:
            return evidence("unknown", "核验连接与原审批机器人不匹配")
        try:
            version = await client.call_action("get_version_info")
            if not isinstance(version, dict) or str(version.get("app_name", "")).casefold() != "snowluma":
                return evidence("unknown", "当前OneBot不是SnowLuma，不能解释原申请标识")
            rows = request_rows(await client.call_action("get_group_system_msg", count=100))
        except (asyncio.TimeoutError, OneBotDisconnected, ConnectionClosed, OSError) as exc:
            return evidence("unavailable", f"请求级核验暂时无法连接（{type(exc).__name__}）")
        except Exception as exc:
            return evidence("unknown", f"请求级核验接口或响应不可用（{type(exc).__name__}），请人工确认")
        matches = [row for row in rows if isinstance(row, dict)
                   and type(row.get("request_id")) is int and str(row["request_id"]) == identity[0]
                   and _decimal(row.get("group_id")) == identity[1]]
        if len(matches) != 1:
            return evidence("unknown", "未找到唯一匹配原群号及申请序号的记录，请人工确认")
        row = matches[0]
        if row.get("flag") != fields["flag"] or _decimal(row.get("requester_uin")) != fields["qq"]:
            return evidence("unknown", "申请QQ或完整SnowLuma标识不匹配，请人工确认")
        if row.get("checked") is not False:
            return evidence("unknown", "该申请已处理或状态不明，不能确定同意或拒绝的实际结果，请人工确认")
        return evidence("unhandled", "SnowLuma精确匹配的原入群申请仍明确未处理")
