"""OneBot V11 websocket client with independent receive and event workers.

Both forward and reverse websocket sessions use the same echo correlation and
cleanup path. A lost acknowledgement is ambiguous, so only explicit failure
responses become DefiniteActionFailure.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from src.gatekeeper.decisions import DefiniteActionFailure

logger = logging.getLogger(__name__)


class OneBotDisconnected(ConnectionError):
    """The transport cannot confirm the outcome of an action."""


class OneBotProtocolError(RuntimeError):
    """The response does not establish whether an action completed."""


class OneBotClient:
    def __init__(self, url: str | None = None, *, self_id: str = "", access_token: str = "",
                 event_handler: Callable[[dict, "OneBotClient"], Awaitable[None]] | None = None,
                 action_timeout: float = 30.0, event_workers: int = 4,
                 event_queue_size: int = 256, reconnect_min: float = 1.0,
                 reconnect_max: float = 30.0):
        self.url = url
        self.self_id = str(self_id)
        self._expected_self_id = str(self_id)
        self.access_token = access_token
        self.event_handler = event_handler
        self.action_timeout = action_timeout
        self.event_workers = max(1, event_workers)
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=max(1, event_queue_size))
        self._pending: dict[str, asyncio.Future] = {}
        self._workers: list[asyncio.Task] = []
        self._runner: asyncio.Task | None = None
        self._transport: Any = None
        self._connected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._closed = False
        self.connection_generation = 0

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._closed

    @property
    def pending_action_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("OneBotClient is closed")
        if not self._workers:
            self._workers = [asyncio.create_task(self._event_worker(), name=f"onebot-event-{i}")
                             for i in range(self.event_workers)]
        if self.url and self._runner is None:
            self._runner = asyncio.create_task(self._reconnect_loop(), name="onebot-forward")

    async def _event_worker(self) -> None:
        while True:
            event = await self._events.get()
            try:
                if self.event_handler:
                    await self.event_handler(event, self)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OneBot event processing failed")
            finally:
                self._events.task_done()

    async def _reconnect_loop(self) -> None:
        delay = self.reconnect_min
        while not self._closed:
            try:
                kwargs: dict[str, Any] = {"open_timeout": max(10.0, self.action_timeout), "max_queue": 32,
                                          "max_size": 2 ** 20}
                if self.access_token:
                    kwargs["extra_headers"] = {"Authorization": f"Bearer {self.access_token}"}
                # websockets 14+ renamed extra_headers in its new asyncio API.
                try:
                    connect = websockets.connect(self.url, **kwargs)
                    async with connect as socket:
                        delay = self.reconnect_min
                        await self.serve(socket)
                except TypeError as exc:
                    if "extra_headers" not in str(exc) or "extra_headers" not in kwargs:
                        raise
                    kwargs["additional_headers"] = kwargs.pop("extra_headers")
                    async with websockets.connect(self.url, **kwargs) as socket:
                        delay = self.reconnect_min
                        await self.serve(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closed:
                    logger.warning("OneBot connection ended (%s); reconnecting", type(exc).__name__)
            if not self._closed:
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max, max(self.reconnect_min, delay * 2))

    async def serve(self, transport: Any) -> None:
        """Run a supplied websocket until disconnected (also used by ASGI reverse WS)."""
        await self.start()
        if self._session_lock.locked():
            raise OneBotDisconnected("another session is already attached")
        async with self._session_lock:
            self._transport = transport
            self.connection_generation += 1
            self._connected.set()
            session_self_id = self._expected_self_id
            try:
                while not self._closed:
                    raw = (await transport.receive_json() if hasattr(transport, "receive_json")
                           else await transport.recv())
                    try:
                        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                    except (ValueError, TypeError):
                        logger.warning("Ignoring invalid OneBot JSON")
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if "echo" in payload:
                        future = self._pending.get(str(payload["echo"]))
                        if future is not None and not future.done():
                            future.set_result(payload)
                        continue
                    if payload.get("self_id") is not None:
                        incoming_id = str(payload["self_id"])
                        if session_self_id and incoming_id != session_self_id:
                            raise OneBotProtocolError("event self_id does not match this websocket session")
                        session_self_id = incoming_id
                        self.self_id = incoming_id
                    if "post_type" in payload:
                        try:
                            self._events.put_nowait(payload)
                        except asyncio.QueueFull:
                            # Stop intake explicitly instead of deadlocking action echoes
                            # behind slow handlers or silently discarding queued requests.
                            logger.error("OneBot event queue full; closing intake connection")
                            raise OneBotDisconnected("event queue capacity exceeded")
            finally:
                self._connected.clear()
                self._transport = None
                self._fail_pending("websocket disconnected before acknowledgement")
                with contextlib.suppress(Exception):
                    await transport.close()

    def _fail_pending(self, detail: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(OneBotDisconnected(detail))

    async def call_action(self, action: str, **params: Any) -> Any:
        if self._closed:
            raise OneBotDisconnected("client is closed")
        # The same deadline bounds connection wait, send-lock contention, socket
        # flow control and the acknowledgement; shutdown also fails all futures.
        return await asyncio.wait_for(self._call_action(action, params), timeout=self.action_timeout)

    async def _call_action(self, action: str, params: dict[str, Any]) -> Any:
        await self._connected.wait()
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with self._send_lock:
                transport = self._transport
                if transport is None:
                    raise OneBotDisconnected("transport disconnected before send")
                payload = {"action": action, "params": params, "echo": echo}
                if hasattr(transport, "send_json"):
                    await transport.send_json(payload)
                else:
                    await transport.send(json.dumps(payload, ensure_ascii=False))
            response = await future
            retcode = response.get("retcode")
            status = response.get("status")
            if status == "failed" or (type(retcode) is int and retcode != 0):
                raise DefiniteActionFailure(f"OneBot action {action} retcode={retcode}")
            if status != "ok" and retcode != 0:
                raise OneBotProtocolError("action response has no definite success status")
            return response.get("data")
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # consume a disconnect racing with a failed send

    async def set_group_add_request(self, **params):
        return await self.call_action("set_group_add_request", **params)

    async def get_stranger_info(self, **params):
        return await self.call_action("get_stranger_info", **params)

    async def get_group_member_info(self, **params):
        return await self.call_action("get_group_member_info", **params)

    async def get_group_member_list(self, **params):
        return await self.call_action("get_group_member_list", **params)

    async def get_group_list(self, **params):
        return await self.call_action("get_group_list", **params)

    async def set_group_kick(self, **params):
        return await self.call_action("set_group_kick", **params)

    async def send_private_msg(self, **params):
        return await self.call_action("send_private_msg", **params)

    async def send_group_msg(self, **params):
        return await self.call_action("send_group_msg", **params)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected.clear()
        self._fail_pending("client shutdown before acknowledgement")
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.close()
        tasks = [*self._workers, *([self._runner] if self._runner else [])]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._runner = None
        while not self._events.empty():
            self._events.get_nowait()
            self._events.task_done()
