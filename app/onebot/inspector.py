"""Select request evidence semantics from the connected OneBot implementation."""
import asyncio

from websockets.exceptions import ConnectionClosed

from app.gatekeeper.decisions import RequestEvidence
from app.onebot.client import OneBotDisconnected
from app.onebot.napcat import NapCatRequestInspector
from app.onebot.snowluma import SnowLumaRequestInspector


class OneBotRequestInspector:
    def __init__(self, get_client):
        self.get_client = get_client

    async def __call__(self, metadata):
        fields = {key: str(metadata.get(key, "")) for key in
                  ("bot_id", "group_id", "qq", "sub_type", "flag", "expected_result")}
        client = self.get_client(fields["bot_id"])
        if client is None:
            return RequestEvidence(state="unavailable", detail="原审批机器人当前未连接", **fields)
        try:
            version = await client.call_action("get_version_info")
        except (asyncio.TimeoutError, OneBotDisconnected, ConnectionClosed, OSError):
            return RequestEvidence(state="unavailable", detail="暂时无法读取OneBot实现身份，稍后重试只读对账", **fields)
        except Exception:
            return RequestEvidence(state="unknown", detail="无法确认OneBot实现身份，请人工确认", **fields)
        name = str(version.get("app_name", "")).casefold() if isinstance(version, dict) else ""
        if name == "snowluma":
            return await SnowLumaRequestInspector(self.get_client)(metadata)
        if "napcat" in name:
            return await NapCatRequestInspector(self.get_client)(metadata)
        return RequestEvidence(state="unknown", detail="当前OneBot实现未提供经过核对的请求级证据，请人工确认", **fields)
