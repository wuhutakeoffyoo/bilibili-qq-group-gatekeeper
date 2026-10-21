"""Lossless event payloads without a bot framework dependency."""
from __future__ import annotations

from typing import Any


class OneBotEvent:
    def __init__(self, payload: dict[str, Any]):
        self.payload = dict(payload)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def dict(self) -> dict[str, Any]:
        return dict(self.payload)

    @property
    def plain_text(self) -> str:
        message = self.payload.get("message", self.payload.get("raw_message", ""))
        if isinstance(message, str):
            return message
        if isinstance(message, list):
            return "".join(str(item.get("data", {}).get("text", ""))
                           for item in message if isinstance(item, dict) and item.get("type") == "text")
        return ""
