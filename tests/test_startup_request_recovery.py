"""Missed requests use the ordinary pipeline and durable exact-request audits."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import ConfigManager
from app.database import legacy
from app.database.records import JoinRequestRecordManager as Records
from app.onebot.client import OneBotClient
from app.onebot.recovery import MissedRequestRecovery, unhandled_events
from app.service import GatekeeperService
from src.gatekeeper.config import GroupConfig, ReviewStageConfig
from src.gatekeeper.models import CheckResult


def row(flag=1, qq=200, group=10, **values):
    return {"request_id": flag, "invitor_uin": qq, "group_id": group,
            "message": "UID:456", "checked": False, **values}


class RecoveryClient:
    connected = True
    self_id = "99"
    connection_generation = 1

    def __init__(self):
        self.rows = [row()]
        self.calls, self.actions, self.messages = [], [], []
        self.online = True
        self.fail_list = False
        self.fail_action = False

    async def call_action(self, action, **params):
        self.calls.append(action)
        if action == "get_status":
            return {"online": self.online, "good": self.online}
        if action == "get_version_info":
            return {"app_name": "NapCat.Onebot"}
        if action == "get_login_info":
            return {"user_id": 99}
        if action == "get_group_system_msg":
            if self.fail_list:
                raise TimeoutError()
            return {"join_requests": self.rows, "invited_requests": [row(900)]}
        raise AssertionError(action)

    async def set_group_add_request(self, **params):
        if not Records.get_pending_decision_count():
            raise AssertionError("missing durable intent")
        self.actions.append(params)
        if self.fail_action:
            raise TimeoutError()

    async def send_private_msg(self, **params):
        self.messages.append(params)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.settings = SimpleNamespace(app_root=Path(self.temp.name), superusers=["1"])
        ConfigManager.reset_state()
        ConfigManager.initialize(self.settings)
        self.group = GroupConfig(group_id="10", review_stages=[ReviewStageConfig(index=1,
            conditions=["bili_account"], on_true=["approve"], on_false=["reject"], on_unknown=["ignore", "notify_admin"])])
        ConfigManager.set_group_config(self.group)
        ConfigManager.set_bili_cookie("test-cookie")
        self.state = "passed"
        self.answers = []

        async def resolve(answer):
            self.answers.append(answer)
            await asyncio.sleep(0)
            return CheckResult(self.state, "test evidence", matched_target_uid=456 if self.state == "passed" else None,
                               matched_name="真实昵称" if self.state == "passed" else None)

        async def run(cookie, operation, **kwargs):
            return await operation(SimpleNamespace(resolve_user_profile=resolve))

        coordinator = SimpleNamespace(run=run, close=AsyncMock())
        self.client = RecoveryClient()
        self.service = GatekeeperService(settings=self.settings, bili_requests=coordinator)
        self.runtime = SimpleNamespace(clients={"forward": self.client}, config=ConfigManager, service=self.service)
        self.recovery = MissedRequestRecovery(self.runtime)

    async def asyncTearDown(self):
        await self.service.close()
        ConfigManager.reset_state()
        legacy.reset_database_state()
        self.env.stop()
        self.temp.cleanup()

    async def test_startup_reviews_only_unchecked_enabled_join_requests(self):
        self.client.rows += [row(2, checked=True), row(3, group=20)]
        await self.recovery.run_once()
        self.assertEqual(self.client.actions, [{"flag": "1", "sub_type": "add", "approve": True}])
        self.assertEqual(self.answers, ["UID:456"])
        audit = Records.get_user_report("200", 5)[2][0]
        self.assertEqual((audit.request_key, audit.bili_name, audit.action_status), ("99:10:add:1", "真实昵称", "applied"))

    async def test_live_event_racing_scan_and_restart_do_not_repeat_any_side_effect(self):
        event = unhandled_events({"join_requests": [row()]}, "99")[0]
        await asyncio.gather(self.recovery.run_once(), self.service.handle_group_request(self.client, event))
        await MissedRequestRecovery(self.runtime).run_once()
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(len(self.answers), 1)
        self.assertEqual(Records.get_user_report("200", 5)[1].approved_count, 1)

    async def test_ignore_is_deduplicated_across_restart_without_spamming_notifications(self):
        self.state = "inaccessible"
        await self.recovery.run_once()
        messages = list(self.client.messages)
        await MissedRequestRecovery(self.runtime).run_once()
        self.assertEqual(self.client.messages, messages)
        self.assertTrue(messages)
        self.assertEqual(Records.get_user_report("200", 5)[1].ignored_count, 1)
        self.assertEqual(self.client.actions, [])

    async def test_rejection_uses_current_pipeline_and_is_not_repeated(self):
        self.state = "failed"
        await self.recovery.run_once()
        await MissedRequestRecovery(self.runtime).run_once()
        self.assertEqual(len(self.client.actions), 1)
        self.assertFalse(self.client.actions[0]["approve"])
        self.assertEqual(Records.get_user_report("200", 5)[1].rejected_count, 1)

    async def test_unknown_ack_keeps_pending_and_scan_does_not_replay_action(self):
        self.client.fail_action = True
        await self.recovery.run_once()
        await MissedRequestRecovery(self.runtime).run_once()
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(Records.get_pending_decision_count(), 1)
        self.assertIsNone(Records.get_bili_binding(456))

    async def test_qq_offline_with_connected_websocket_then_online_triggers_scan(self):
        self.client.online = False
        await self.recovery.run_once()
        self.assertNotIn("get_group_system_msg", self.client.calls)
        self.client.online = True
        await self.recovery.run_once()
        await self.recovery.run_once()
        self.assertEqual(self.client.calls.count("get_group_system_msg"), 1)
        self.client.online = False
        await self.recovery.run_once()
        self.client.online = True
        await self.recovery.run_once()
        self.assertEqual(self.client.calls.count("get_group_system_msg"), 2)
        self.assertEqual(len(self.client.actions), 1)

    async def test_brief_transport_reconnect_triggers_scan_without_observed_offline(self):
        await self.recovery.run_once()
        self.client.connection_generation += 1
        self.client.rows = [row(2, qq=201)]
        await self.recovery.run_once()
        self.assertEqual(self.client.calls.count("get_group_system_msg"), 2)
        self.assertEqual([action["flag"] for action in self.client.actions], ["1", "2"])

    async def test_periodic_scan_catches_missed_push_on_healthy_connection(self):
        now = [0.0]
        self.recovery = MissedRequestRecovery(self.runtime, clock=lambda: now[0])
        await self.recovery.run_once()
        self.client.rows.append(row(2, qq=201))
        now[0] = 59
        await self.recovery.run_once()
        self.assertEqual(self.client.calls.count("get_group_system_msg"), 1)
        now[0] = 60
        await self.recovery.run_once()
        self.assertEqual([action["flag"] for action in self.client.actions], ["1", "2"])
        self.assertEqual(Records.get_user_report("200", 5)[1].approved_count, 1)

    async def test_failed_periodic_review_retries_before_next_interval(self):
        now = [0.0]
        self.recovery = MissedRequestRecovery(self.runtime, clock=lambda: now[0])
        await self.recovery.run_once()
        self.client.rows.append(row(2, qq=201))
        now[0] = 60
        with patch.object(self.service, "handle_group_request", side_effect=RuntimeError("storage unavailable")):
            await self.recovery.run_once()
        now[0] = 61
        await self.recovery.run_once()
        self.assertEqual([action["flag"] for action in self.client.actions], ["1", "2"])

    async def test_failed_periodic_list_retries_before_next_interval(self):
        now = [0.0]
        self.recovery = MissedRequestRecovery(self.runtime, clock=lambda: now[0])
        await self.recovery.run_once()
        now[0] = 60
        self.client.fail_list = True
        await self.recovery.run_once()
        self.client.fail_list = False
        self.client.rows.append(row(2, qq=201))
        now[0] = 61
        await self.recovery.run_once()
        self.assertEqual([action["flag"] for action in self.client.actions], ["1", "2"])

    async def test_unknown_implementation_is_not_polled_until_reconnect(self):
        now = [0.0]
        self.recovery = MissedRequestRecovery(self.runtime, clock=lambda: now[0])
        original = self.client.call_action

        async def unknown_version(action, **kwargs):
            if action == "get_version_info":
                self.client.calls.append(action)
                return {"app_name": "unknown"}
            return await original(action, **kwargs)

        self.client.call_action = unknown_version
        await self.recovery.run_once()
        now[0] = 120
        await self.recovery.run_once()
        self.assertEqual(self.client.calls.count("get_version_info"), 1)
        self.assertNotIn("get_group_system_msg", self.client.calls)
        self.client.call_action = original
        self.client.connection_generation += 1
        await self.recovery.run_once()
        self.assertEqual(len(self.client.actions), 1)

    async def test_real_onebot_echo_session_drives_startup_and_reconnect_scan(self):
        source = self.client

        class Transport:
            def __init__(self):
                self.incoming = asyncio.Queue()

            async def receive_json(self):
                return await self.incoming.get()

            async def send_json(self, payload):
                if payload["action"] == "set_group_add_request":
                    await source.set_group_add_request(**payload["params"])
                    data = None
                else:
                    data = await source.call_action(payload["action"], **payload["params"])
                await self.incoming.put({"status": "ok", "retcode": 0, "data": data, "echo": payload["echo"]})

            async def close(self):
                pass

        client = OneBotClient(self_id="99", action_timeout=2)
        self.runtime.clients = {"reverse:99": client}
        try:
            for generation in (1, 2):
                session = asyncio.create_task(client.serve(Transport()))
                await asyncio.wait_for(client._connected.wait(), 2)
                self.assertEqual(client.connection_generation, generation)
                await self.recovery.run_once()
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)
            self.assertEqual(len(source.actions), 1)
            self.assertEqual(source.calls.count("get_group_system_msg"), 2)
        finally:
            await client.close()

    async def test_failed_list_retries_and_disconnected_client_waits(self):
        self.client.connected = False
        await self.recovery.run_once()
        self.assertEqual(self.client.calls, [])
        self.client.connected = True
        self.client.fail_list = True
        await self.recovery.run_once()
        self.client.fail_list = False
        await self.recovery.run_once()
        self.assertEqual(len(self.client.actions), 1)

    async def test_bad_login_or_schema_never_creates_an_approval(self):
        original = self.client.call_action

        async def wrong_login(action, **kwargs):
            return {"user_id": 100} if action == "get_login_info" else await original(action, **kwargs)

        self.client.call_action = wrong_login
        await self.recovery.run_once()
        self.assertEqual(self.client.actions, [])
        self.client.call_action = original
        await self.recovery.run_once()
        self.assertEqual(len(self.client.actions), 1)

    def test_parser_rejects_ambiguous_unsafe_and_malformed_rows(self):
        rows = [row(1), row(1, qq=201), row(2, checked=True), row(3, checked=0),
                row(2**53), row(5, group=False), row(6, qq=0), row(7, message=None),
                row("8"), row(9)]
        self.assertEqual([event["flag"] for event in unhandled_events({"join_requests": rows}, "99")], ["9"])
        with self.assertRaises(ValueError):
            unhandled_events({}, "99")

    async def test_failed_review_does_not_block_other_rows_and_retries_next_cycle(self):
        self.client.rows = [row(1), row(2, qq=201)]
        original = self.service.handle_group_request

        async def fail_one(client, event):
            if event["flag"] == "1":
                raise RuntimeError("temporary storage outage")
            await original(client, event)

        with patch.object(self.service, "handle_group_request", side_effect=fail_one):
            await self.recovery.run_once()
        self.assertEqual([action["flag"] for action in self.client.actions], ["2"])
        await self.recovery.run_once()
        self.assertEqual([action["flag"] for action in self.client.actions], ["2", "1"])
