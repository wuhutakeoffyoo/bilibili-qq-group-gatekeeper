"""Catch up missed NapCat group-add events after startup or reconnection."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter

from app.onebot.napcat import MAX_SAFE_INTEGER, _decimal

logger = logging.getLogger(__name__)
REQUEST_LIMIT = 100


def unhandled_events(payload, bot_id):
    """Only exact, unambiguous unchecked join rows may become add events."""
    rows = payload.get("join_requests") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("NapCat join request list is missing")
    candidates = []
    identities = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        flag = row.get("request_id")
        group, qq = _decimal(row.get("group_id")), _decimal(row.get("invitor_uin"))
        if type(flag) is not int or not 0 < flag <= MAX_SAFE_INTEGER or group is None:
            continue
        identity = (group, str(flag))
        identities[identity] += 1
        if qq is None or row.get("checked") is not False or not isinstance(row.get("message"), str):
            continue
        candidates.append((identity, {"post_type": "request", "request_type": "group", "sub_type": "add",
            "self_id": int(bot_id), "group_id": int(group), "user_id": int(qq),
            "flag": str(flag), "comment": row["message"]}))
    return [event for identity, event in candidates if identities[identity] == 1]


class MissedRequestRecovery:
    def __init__(self, runtime):
        self.runtime = runtime
        self._checked = {}
        self._lock = asyncio.Lock()

    async def run_once(self):
        """Read status every cycle; scan once per healthy transport/login period.

        A WebSocket can remain connected when QQ goes offline. Status failures
        therefore reset the scan marker too. Failed scans retry next cycle.
        """
        async with self._lock:
            clients = list(self.runtime.clients.values())
            self._checked = {client: generation for client, generation in self._checked.items() if client in clients}
            for client in clients:
                if not client.connected:
                    self._checked.pop(client, None)
                    continue
                generation = getattr(client, "connection_generation", 0)
                try:
                    status = await client.call_action("get_status")
                    if not isinstance(status, dict) or status.get("online") is not True or status.get("good") is not True:
                        if client in self._checked:
                            logger.warning("QQ went offline or unhealthy; catch-up will resume when online")
                        self._checked.pop(client, None)
                        continue
                    if self._checked.get(client) == generation:
                        continue
                    version = await client.call_action("get_version_info")
                    if not isinstance(version, dict) or "napcat" not in str(version.get("app_name", "")).casefold():
                        logger.warning("Startup request scan requires NapCat get_group_system_msg support")
                        self._checked[client] = generation
                        continue
                    login = await client.call_action("get_login_info")
                    bot_id = _decimal(login.get("user_id")) if isinstance(login, dict) else None
                    if bot_id is None or (client.self_id and str(client.self_id) != bot_id):
                        raise ValueError("OneBot login identity does not match this connection")
                    client.self_id = bot_id
                    payload = await client.call_action("get_group_system_msg", count=REQUEST_LIMIT)
                    events = unhandled_events(payload, bot_id)
                    total = sum(len(payload.get(key, [])) for key in ("join_requests", "invited_requests")
                                if isinstance(payload.get(key), list))
                    if total >= REQUEST_LIMIT:
                        logger.warning("NapCat system-message window reached %s; older requests may require manual review", REQUEST_LIMIT)
                    processed = 0
                    failed = False
                    for event in events:
                        if not client.connected or getattr(client, "connection_generation", 0) != generation:
                            raise ConnectionError("OneBot connection changed during startup request scan")
                        group = self.runtime.config.get_group_config(str(event["group_id"]))
                        if group is None or not group.enabled:
                            continue
                        try:
                            await self.runtime.service.handle_group_request(client, event)
                            processed += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            failed = True
                            logger.exception("Missed request review failed; will retry next cycle")
                    if not failed:
                        self._checked[client] = generation
                    logger.info("Startup/reconnect request scan: bot=%s unchecked=%s reviewed=%s retry=%s",
                                bot_id, len(events), processed, failed)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._checked.pop(client, None)
                    logger.warning("Startup request scan unavailable (%s); retry next cycle", type(exc).__name__)

    async def loop(self):
        while True:
            await self.run_once()
            await asyncio.sleep(30)
