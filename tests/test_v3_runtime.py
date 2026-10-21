"""Standalone application workflows against actual temporary SQLite storage."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn
import websockets

from app.commands import CommandService
from app.config import ConfigManager
from app.database import legacy
from app.database.leaves import LeaveRecordManager
from app.database.records import JoinRequestRecordManager
from app.runtime import Runtime
from app.service import GatekeeperService
from src.gatekeeper.config import GroupConfig, ReviewStageConfig
from src.gatekeeper.models import CheckResult


class FakeApi:
    def __init__(self):
        self.answers = []

    async def resolve_user_profile(self, answer):
        self.answers.append(answer)
        if answer == "wrong":
            return CheckResult("failed", "未搜索到该B站昵称，请检查昵称或填写正确UID。")
        value = str(answer).split(":")[-1].strip()
        return CheckResult("passed", "账号存在", matched_target_uid=int(value), matched_level=6)

    async def check_follow_any_status(self, *args, **kwargs):
        return CheckResult("passed", "已关注")

    async def check_medal_any_status(self, *args, **kwargs):
        return CheckResult("passed", "已满足", matched_level=10)


class FakeBiliCoordinator:
    def __init__(self):
        self.api = FakeApi()
        self.closed = False

    async def run(self, cookie, operation, **kwargs):
        return await operation(self.api)

    def common_negative_is_definitive(self, *args):
        return False

    async def close(self):
        self.closed = True


class FakeClient:
    self_id = "99"
    connected = True

    def __init__(self):
        self.actions = []
        self.messages = []
        self.fail = False
        self.closed = False

    async def set_group_add_request(self, **params):
        # The external action must never run before its durable intent exists.
        if JoinRequestRecordManager.get_pending_decision_count() < 1:
            raise AssertionError("action ran before durable pending intent")
        self.actions.append(params)
        if self.fail:
            raise TimeoutError("acknowledgement lost")

    async def get_stranger_info(self, **params):
        return {"level": 42}

    async def get_group_member_info(self, **params):
        return {"nickname": "departed"}

    async def send_private_msg(self, **params):
        self.messages.append(params)

    async def start(self):
        return None

    async def close(self):
        self.connected = False
        self.closed = True


class FakeExtension:
    def __init__(self):
        self.closed = False
        self.left = []

    async def before_decision(self, context):
        return None

    async def on_group_decrease(self, group, qq):
        self.left.append((group, qq))

    async def loop(self):
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class RuntimeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.system_env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE") if name in os.environ}
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        ConfigManager.reset_state()
        self.settings = SimpleNamespace(app_root=self.root, superusers=["1"], command_start=["/", ""],
                                        onebot_ws_urls=[], onebot_access_token="test-token")
        ConfigManager.initialize(self.settings)
        self.group = GroupConfig(group_id="10", required_bili_level=3, required_qq_level=32,
                                 review_stages=[ReviewStageConfig(index=1, name="账号审核",
                                    conditions=["bili_account", "bili_level", "qq_level", "no_conflict", "no_identity_change"],
                                    on_true=["approve"], on_false=["reject", "notify_admin"], on_unknown=["ignore"])])
        ConfigManager.set_group_config(self.group)
        ConfigManager.set_bili_cookie("SESSDATA=test")
        self.bili = FakeBiliCoordinator()
        self.client = FakeClient()
        self.service = GatekeeperService(settings=self.settings, bili_requests=self.bili,
                                         client_provider=lambda: self.client)
        self.ownership = FakeExtension()
        self.patrol = FakeExtension()
        self.credentials = FakeExtension()
        self.runtime = Runtime(settings=self.settings, service=self.service, clients={"test": self.client},
                               ownership=self.ownership, patrol=self.patrol, credentials=self.credentials,
                               background_enabled=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        ConfigManager.reset_state()
        legacy.reset_database_state()
        self.environment.stop()
        self.temp.cleanup()

    def request(self, *, qq="200", answer="456", flag="request-1"):
        return {"post_type": "request", "request_type": "group", "sub_type": "add", "self_id": 99,
                "group_id": 10, "user_id": int(qq), "comment": answer, "flag": flag}

    async def test_event_pipeline_writes_intent_then_action_then_binding(self):
        await self.runtime.handle_event(self.request(), self.client)
        self.assertTrue(self.client.actions[0]["approve"])
        self.assertEqual(JoinRequestRecordManager.get_pending_decision_count(), 0)
        binding = JoinRequestRecordManager.get_bili_binding(456)
        self.assertEqual(binding.owner_qq, "200")
        _, summary, audits = JoinRequestRecordManager.get_user_report("200", 5)
        self.assertEqual(summary.approved_count, 1)
        self.assertEqual(audits[0].action_status, "applied")
        self.assertEqual(audits[0].condition_states["bili_account"], "true")
        self.assertTrue(audits[0].stage_results)

    async def test_unknown_action_is_pending_only_for_related_identity(self):
        self.client.fail = True
        await self.runtime.handle_event(self.request(), self.client)
        self.assertEqual(JoinRequestRecordManager.get_pending_decision_count(), 1)
        self.assertIsNone(JoinRequestRecordManager.get_bili_binding(456))
        self.client.fail = False
        await self.runtime.handle_event(self.request(qq="201", answer="457", flag="other"), self.client)
        self.assertEqual(JoinRequestRecordManager.get_bili_binding(457).owner_qq, "201")
        await self.runtime.handle_event(self.request(qq="202", answer="456", flag="same-uid"), self.client)
        self.assertEqual(len(self.client.actions), 2)

    async def test_pipeline_without_conflict_condition_keeps_explicit_legacy_opt_out(self):
        JoinRequestRecordManager.add_audit(group_id='20', qq='100', bili_uid=456,
                                           result='approved', reasons=['existing owner fixture'])
        group = self.group.model_copy(deep=True)
        group.review_stages = [ReviewStageConfig(index=1, conditions=['bili_account'],
                                                on_true=['approve'], on_false=['reject'], on_unknown=['ignore'])]
        ConfigManager.set_group_config(group)
        await self.runtime.handle_event(self.request(qq='200', answer='456', flag='opt-out'), self.client)
        self.assertTrue(self.client.actions[-1]['approve'])
        self.assertEqual(JoinRequestRecordManager.get_bili_binding(456).owner_qq, '100')
        self.assertEqual(JoinRequestRecordManager.get_user_report('200', 1)[2][0].result, 'approved')

    async def test_default_reconciliation_keeps_unsupported_result_for_manual_confirmation(self):
        self.client.fail = True
        await self.runtime.handle_event(self.request(), self.client)
        self.client.fail = False
        results = await self.runtime.reconciler.run_once()
        self.assertEqual(list(results.values()), ["manual"])
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(JoinRequestRecordManager.get_pending_decision_count(), 1)
        self.assertIsNone(JoinRequestRecordManager.get_bili_binding(456))

    async def test_bad_nickname_rejection_keeps_correction_in_action_audit_and_notification(self):
        await self.runtime.handle_event(self.request(answer="wrong"), self.client)
        action = self.client.actions[0]
        self.assertFalse(action["approve"])
        self.assertIn("未搜索到", action["reason"])
        self.assertIn("请不要填写B站昵称或UID以外的内容", action["reason"])
        _, _, audits = JoinRequestRecordManager.get_user_report("200", 5)
        self.assertIn("未搜索到", "；".join(audits[0].reasons))
        self.assertTrue(any("未搜索到" in message["message"] for message in self.client.messages))

    async def test_bare_qq_mistake_avoids_http_but_explicit_uid_is_allowed(self):
        await self.runtime.handle_event(self.request(answer="200"), self.client)
        self.assertIn("可能误填了QQ号", self.client.actions[0]["reason"])
        self.assertEqual(self.bili.api.answers, [])
        await self.runtime.handle_event(self.request(answer="UID:200", flag="explicit"), self.client)
        self.assertTrue(self.client.actions[-1]["approve"])
        self.assertEqual(self.bili.api.answers, ["UID:200"])

    async def test_simulation_is_read_only_and_does_not_use_admin_qq_as_applicant(self):
        before = JoinRequestRecordManager.get_user_report("200", 5)
        result = await self.service.simulate("10", "200")
        self.assertEqual(self.bili.api.answers, ["200"])
        self.assertEqual(result["condition_states"]["qq_level"], "unknown")
        self.assertEqual(JoinRequestRecordManager.get_user_report("200", 5), before)
        self.assertEqual(self.client.actions, [])
        complete = await self.service.simulate("10", "456", qq="200")
        self.assertEqual(complete["action"], "approve")
        self.assertIsNone(JoinRequestRecordManager.get_bili_binding(456))

    async def test_command_permission_silence_casefold_and_neutral_early_reply(self):
        commands = CommandService(self.service)
        def message(qq, text, kind="private"):
            return {"message_type": kind, "user_id": qq, "message": text}
        self.assertIsNone(await commands.dispatch("2", message(2, "/help")))
        self.assertIsNone(await commands.dispatch("1", message(1, "/help", "group")))
        self.assertIn("可用指令", await commands.dispatch("1", message(1, "/HeLP")))
        version = ConfigManager.get_version()
        self.assertIn("用法", await commands.dispatch("1", message(1, "/设置群")))
        self.assertEqual(ConfigManager.get_version(), version)
        self.assertEqual(await commands.dispatch("1", message(1, "/设置群 10 required_qq_level -1")), "required_qq_level 不能小于 0")
        self.assertEqual(ConfigManager.get_version(), version)
        reply = await commands.dispatch("1", message(1, "/设置群 10 required_qq_level 35"))
        self.assertIn("配置已更新", reply)
        ConfigManager.reset_state()
        ConfigManager.initialize(self.settings)
        self.assertEqual(ConfigManager.get_group_config("10").required_qq_level, 35)
        self.assertNotIn("SESSDATA", await commands.dispatch("1", message(1, "/查看配置")))

    async def test_administrator_qr_reply_contains_a_decodable_png_without_pillow(self):
        import base64
        import png
        self.service.credentials = SimpleNamespace(start_qr_login=AsyncMock(return_value={
            'url': 'https://example.invalid/native-login', 'session_id': 'fixture-session'}))
        commands = CommandService(self.service)
        reply = await commands.dispatch('1', {'message_type': 'private', 'user_id': 1, 'message': '/登录二维码'})
        encoded = reply.split('[CQ:image,file=base64://', 1)[1].split(']', 1)[0]
        image = base64.b64decode(encoded, validate=True)
        self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
        width, height, rows, info = png.Reader(bytes=image).read()
        self.assertEqual(width, height)
        self.assertGreater(width, 100)
        self.assertEqual(len(list(rows)), height)
        self.assertEqual(info['bitdepth'], 1)

    async def test_decrease_records_departure_and_invokes_policy_hook(self):
        await self.runtime.handle_event({"post_type": "notice", "notice_type": "group_decrease", "group_id": 10, "user_id": 200}, self.client)
        self.assertIsNotNone(LeaveRecordManager.get_leave_record("10", "200"))
        self.assertEqual(self.patrol.left, [("10", "200")])

    async def test_lifecycle_closes_injected_clients_extensions_and_http_coordinator(self):
        self.runtime.spawn(asyncio.Event().wait(), name="test-background")
        await self.runtime.close()
        self.assertFalse(self.runtime.started)
        self.assertEqual(self.runtime._tasks, set())
        self.assertTrue(self.client.closed)
        self.assertTrue(self.bili.closed)
        self.assertTrue(self.credentials.closed)
        self.assertTrue(self.ownership.closed)
        self.assertTrue(self.patrol.closed)

    async def test_expired_cookie_private_alert_deduplicates_but_network_error_is_not_expiry(self):
        from app.bilibili.monitor import CookieCheckDetail, CookieCheckResult

        with patch("app.bilibili.monitor.run_cookie_check_once", AsyncMock(return_value=CookieCheckDetail(CookieCheckResult.EXPIRED))):
            await self.runtime.check_cookie_once()
            await self.runtime.check_cookie_once()
        self.assertEqual(len(self.client.messages), 1)
        self.assertIn("明确失效", self.client.messages[0]["message"])
        with patch("app.bilibili.monitor.run_cookie_check_once", AsyncMock(return_value=CookieCheckDetail(CookieCheckResult.NETWORK_ERROR))):
            await self.runtime.check_cookie_once()
        self.assertEqual(len(self.client.messages), 2)
        self.assertIn("无法可靠判断", self.client.messages[-1]["message"])
        with patch("app.bilibili.monitor.run_cookie_check_once", AsyncMock(return_value=CookieCheckDetail(CookieCheckResult.VALID))):
            await self.runtime.check_cookie_once()
        with patch("app.bilibili.monitor.run_cookie_check_once", AsyncMock(return_value=CookieCheckDetail(CookieCheckResult.EXPIRED))):
            await self.runtime.check_cookie_once()
        self.assertEqual(len(self.client.messages), 3)

    async def test_single_asgi_health_routes_use_injected_runtime(self):
        from app.main import create_app

        await self.runtime.close()
        self.client = FakeClient()
        self.bili = FakeBiliCoordinator()
        self.service = GatekeeperService(settings=self.settings, bili_requests=self.bili,
                                         client_provider=lambda: self.client)
        self.runtime = Runtime(settings=self.settings, service=self.service, clients={"test": self.client},
                               ownership=FakeExtension(), patrol=FakeExtension(), credentials=FakeExtension(),
                               background_enabled=False)
        application = create_app(self.runtime)
        self.assertFalse(self.runtime.started)
        async with application.router.lifespan_context(application):
            self.assertTrue(self.runtime.started)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
                response = await client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["onebot_connected"], 1)
        self.assertIs(application.state.runtime, self.runtime)
        self.assertFalse(self.runtime.started)
        self.assertTrue(self.client.closed)
        self.assertTrue(self.bili.closed)

    async def test_default_imports_do_not_require_nonebot(self):
        script = """import sys
class BlockNoneBot:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'nonebot' or fullname.startswith('nonebot.'):
            raise AssertionError('default runtime imported NoneBot: ' + fullname)
sys.meta_path.insert(0, BlockNoneBot())
from app.main import create_app
from app.commands import CommandService
from app.service import GatekeeperService
assert not any(name == 'nonebot' or name.startswith('nonebot.') for name in sys.modules)
"""
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script],
                                         capture_output=True, text=True, timeout=20,
                                         env=self.system_env,
                                         cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_actual_reverse_websocket_event_to_sqlite_and_shutdown(self):
        from app.main import create_app

        application = create_app(self.runtime)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            with self.assertRaises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://127.0.0.1:{port}/onebot/v11/ws", additional_headers={"X-Self-ID": "99"}):
                    self.fail("unauthorized websocket accepted")
            async with websockets.connect(f"ws://127.0.0.1:{port}/onebot/v11/ws", additional_headers={
                "X-Self-ID": "99", "Authorization": "Bearer test-token"}) as connection:
                await connection.send(json.dumps(self.request()))
                call = json.loads(await asyncio.wait_for(connection.recv(), 2))
                self.assertEqual(call["action"], "get_stranger_info")
                await connection.send(json.dumps({"status": "ok", "retcode": 0, "data": {"level": 42}, "echo": call["echo"]}))
                call = json.loads(await asyncio.wait_for(connection.recv(), 2))
                self.assertEqual(call["action"], "set_group_add_request")
                self.assertTrue(call["params"]["approve"])
                self.assertEqual(JoinRequestRecordManager.get_pending_decision_count(), 1)
                await connection.send(json.dumps({"status": "ok", "retcode": 0, "data": None, "echo": call["echo"]}))
                for _ in range(100):
                    binding = JoinRequestRecordManager.get_bili_binding(456)
                    if binding is not None:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(binding.owner_qq, "200")
            for _ in range(100):
                if "reverse:99" not in self.runtime.clients:
                    break
                await asyncio.sleep(.01)
            self.assertNotIn("reverse:99", self.runtime.clients)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            listener.close()


if __name__ == "__main__":
    unittest.main()
