"""Real local websocket tests for the standalone OneBot transport."""
import asyncio
import contextlib
import json
import unittest

import websockets

from app.onebot.client import OneBotClient, OneBotDisconnected
from app.onebot.napcat import NapCatRequestInspector
from src.gatekeeper.decisions import DefiniteActionFailure


class OneBotTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.servers = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()

    async def connect(self, handler, **kwargs):
        server = await websockets.serve(handler, "127.0.0.1", 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(f"ws://127.0.0.1:{port}", reconnect_min=.01, reconnect_max=.05, **kwargs)
        self.clients.append(client)
        await client.start()
        await asyncio.wait_for(client._connected.wait(), 2)
        return client

    async def test_event_handler_can_wait_for_echo_without_blocking_receive(self):
        finished = asyncio.Event()
        observed = []

        async def server(socket):
            await socket.send(json.dumps({"post_type": "request", "self_id": 123, "user_id": 456}))
            payload = json.loads(await socket.recv())
            observed.append(payload)
            await socket.send(json.dumps({"status": "ok", "retcode": 0, "data": {"level": 42}, "echo": payload["echo"]}))
            await socket.wait_closed()

        async def on_event(event, client):
            result = await client.get_stranger_info(user_id=event["user_id"])
            self.assertEqual(result["level"], 42)
            finished.set()

        client = await self.connect(server, event_handler=on_event, action_timeout=.5)
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(client.self_id, "123")
        self.assertEqual(observed[0]["action"], "get_stranger_info")
        self.assertEqual(client.pending_action_count, 0)

    async def test_concurrent_replies_are_correlated_by_echo(self):
        async def server(socket):
            calls = [json.loads(await socket.recv()), json.loads(await socket.recv())]
            for call in reversed(calls):
                await socket.send(json.dumps({"status": "ok", "retcode": 0, "data": call["params"], "echo": call["echo"]}))
            await socket.wait_closed()

        client = await self.connect(server, action_timeout=1)
        first, second = await asyncio.gather(client.call_action("a", marker=1), client.call_action("b", marker=2))
        self.assertEqual(first, {"marker": 1})
        self.assertEqual(second, {"marker": 2})
        self.assertEqual(client.pending_action_count, 0)

    async def test_explicit_failure_is_definite_and_disconnect_is_ambiguous(self):
        async def server(socket):
            call = json.loads(await socket.recv())
            await socket.send(json.dumps({"status": "failed", "retcode": 1200, "echo": call["echo"]}))
            await socket.recv()
            await socket.close()

        client = await self.connect(server, action_timeout=.5)
        with self.assertRaises(DefiniteActionFailure):
            await client.set_group_add_request(flag="f", sub_type="add", approve=True)
        with self.assertRaises(OneBotDisconnected):
            await client.set_group_add_request(flag="g", sub_type="add", approve=False)
        self.assertEqual(client.pending_action_count, 0)

    async def test_timeout_removes_pending_future_and_late_echo_does_not_break_connection(self):
        async def server(socket):
            call = json.loads(await socket.recv())
            await asyncio.sleep(.1)
            await socket.send(json.dumps({"status": "ok", "retcode": 0, "echo": call["echo"]}))
            call = json.loads(await socket.recv())
            await socket.send(json.dumps({"status": "ok", "retcode": 0, "data": "alive", "echo": call["echo"]}))
            await socket.wait_closed()

        client = await self.connect(server, action_timeout=.03)
        with self.assertRaises(asyncio.TimeoutError):
            await client.call_action("slow")
        self.assertEqual(client.pending_action_count, 0)
        await asyncio.sleep(.12)
        self.assertEqual(await client.call_action("next"), "alive")

    async def test_reconnect_and_access_token(self):
        connections = 0
        headers = []
        reconnected = asyncio.Event()

        async def server(socket):
            nonlocal connections
            connections += 1
            header = getattr(getattr(socket, "request", None), "headers", None) or getattr(socket, "request_headers", {})
            headers.append(header.get("Authorization"))
            if connections == 1:
                await socket.close()
            else:
                reconnected.set()
                await socket.wait_closed()

        client = await self.connect(server, access_token="test-token")
        await asyncio.wait_for(reconnected.wait(), 2)
        await asyncio.wait_for(client._connected.wait(), 2)
        self.assertTrue(client.connected)
        self.assertEqual(headers[:2], ["Bearer test-token", "Bearer test-token"])
        await client.close()
        self.assertEqual(client._workers, [])
        self.assertIsNone(client._runner)

    async def test_queue_capacity_is_bounded_and_shutdown_cancels_handlers(self):
        blocked = asyncio.Event()
        cancelled = asyncio.Event()

        async def on_event(event, client):
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def server(socket):
            await socket.send(json.dumps({"post_type": "notice", "n": 1}))
            await blocked.wait()
            for number in (2, 3, 4):
                with contextlib.suppress(websockets.ConnectionClosed):
                    await socket.send(json.dumps({"post_type": "notice", "n": number}))
            await socket.wait_closed()

        client = await self.connect(server, event_handler=on_event, event_workers=1, event_queue_size=1)
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.sleep(.05)
        self.assertLessEqual(client._events.qsize(), 1)
        await client.close()
        self.assertTrue(cancelled.is_set())


class NapCatEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_an_exact_explicitly_unhandled_safe_request_can_be_retried(self):
        metadata = dict(bot_id="99", group_id="10", qq="200", sub_type="add", flag="123", expected_result="approved")
        row = dict(request_id=123, invitor_uin=200, group_id=10, checked=False)

        class Client:
            self_id = "99"
            app_name = "NapCat.Onebot"
            rows = [row]
            unavailable = False
            timeout = False
            disconnected = False

            async def call_action(self, action, **params):
                if self.timeout:
                    raise asyncio.TimeoutError()
                if self.disconnected:
                    raise OneBotDisconnected("connection lost")
                if self.unavailable:
                    raise RuntimeError("unsupported")
                if action == "get_version_info":
                    return {"app_name": self.app_name}
                self.asserted_count = params["count"]
                return {"join_requests": self.rows}

        client = Client()
        inspector = NapCatRequestInspector(lambda bot_id: client)
        evidence = await inspector(metadata)
        self.assertEqual(evidence.state, "unhandled")
        self.assertTrue(evidence.matches(metadata))
        self.assertEqual(client.asserted_count, 100)
        variants = [
            ({**row, "request_id": 124}, "wrong flag"),
            ({**row, "group_id": 11}, "wrong group"),
            ({**row, "invitor_uin": 201}, "wrong applicant"),
            ({**row, "checked": True}, "processed does not prove applied"),
            ({**row, "checked": 0}, "integer is not explicit false"),
            ({**row, "request_id": True}, "boolean request id"),
            ({**row, "request_id": 123.0}, "non-integer JSON request id"),
            ({**row, "request_id": 2 ** 53}, "precision risk"),
        ]
        for changed, label in variants:
            with self.subTest(label=label):
                client.rows = [changed]
                self.assertEqual((await inspector(metadata)).state, "unknown")
        client.rows = []
        self.assertEqual((await inspector(metadata)).state, "unknown")
        client.rows = [row, row]
        self.assertEqual((await inspector(metadata)).state, "unknown")
        client.rows = [row]
        client.app_name = "other implementation"
        self.assertEqual((await inspector(metadata)).state, "unknown")
        client.app_name = "NapCat"
        client.unavailable = True
        self.assertEqual((await inspector(metadata)).state, "unknown")
        client.unavailable = False
        client.timeout = True
        self.assertEqual((await inspector(metadata)).state, "unavailable")
        client.timeout = False
        client.disconnected = True
        self.assertEqual((await inspector(metadata)).state, "unavailable")
        client.disconnected = False
        client.self_id = "100"
        self.assertEqual((await inspector(metadata)).state, "unknown")
        client.self_id = "99"
        self.assertEqual((await inspector({**metadata, "flag": str(2 ** 53)})).state, "unknown")
        self.assertEqual((await NapCatRequestInspector(lambda bot_id: None)(metadata)).state, "unavailable")


if __name__ == "__main__":
    unittest.main()
