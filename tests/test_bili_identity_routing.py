"""Input routing and handler smoke tests, with real SQLite and a recorded HTTP boundary."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot

nonebot.init()

from src.gatekeeper.decisions import DecisionService
from src.gatekeeper.conditions import LEGACY_NICKNAME_NOT_FOUND_MESSAGE
from src.gatekeeper.parsing import parse_bili_identity
from src.plugins.group_request_manager import database, decision_adapter, main as manager
from src.plugins.group_request_manager.bili_api import (
    BiliApi,
    UID_INVALID_MESSAGE,
    reset_wbi_key_cache,
)
from src.plugins.group_request_manager.config import ConfigManager, GroupConfig, ReviewStageConfig
from src.plugins.group_request_manager.request_record import JoinRequestRecordManager as Records
from nonebot.exception import FinishedException


UID = 123456
NAME = "Example_User123"


class IdentityParsingTests(unittest.TestCase):
    def test_uid_cleanup_and_nickname_preservation(self):
        cases = [
            ("123456", "uid", "123456"),
            ("１２３４５６", "uid", "123456"),
            ("\u200b123456\ufeff", "uid", "123456"),
            ("UID:123456", "uid", "123456"),
            ("uid123456", "uid", "123456"),
            ("B站UID：１２３，４５６", "uid", "123456"),
            ("ＵＩＤ = 123 456", "uid", "123456"),
            ("（UID：'123_456'）", "uid", "123456"),
            ("UID：１２３４５６", "uid", "123456"),
            ("UID:abc123", "uid", "abc123"),
            ("UID:-123", "uid", "-123"),
            ("UID:1 UID:2", "uid", ""),
            ("Example_User123", "nickname", "Example_User123"),
            ("fluid123", "nickname", "fluid123"),
            ("UID爱好者123", "nickname", "UID爱好者123"),
            ("１２Ａlice", "nickname", "１２Ａlice"),
            (" 昵称 带空格123 ", "nickname", "昵称 带空格123"),
            ("答案：Example_User123", "nickname", "答案：Example_User123"),
        ]
        for raw, kind, value in cases:
            with self.subTest(raw=raw):
                actual = parse_bili_identity(raw)
                self.assertEqual((actual.kind, actual.value), (kind, value))


class UidProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_uid_response_requires_matching_identity_and_valid_level(self):
        cases = [
            ({"code": 0, "data": {"card": {"mid": str(UID), "level_info": {"current_level": 0}}}}, "passed"),
            ({"code": -404}, "failed"),
            ({"code": -352}, "inaccessible"),
            ({"code": 0, "data": None}, "inaccessible"),
            ({"code": 0, "data": {"card": {"mid": UID + 1, "level_info": {"current_level": 6}}}}, "inaccessible"),
            ({"code": 0, "data": {"card": {"mid": UID, "level_info": {"current_level": True}}}}, "inaccessible"),
            ({"code": 0, "data": {"card": {"mid": UID, "level_info": {"current_level": -1}}}}, "inaccessible"),
        ]
        for payload, state in cases:
            with self.subTest(payload=payload):
                api = BiliApi(client=object())
                api._get = AsyncMock(return_value=payload)
                result = await api.get_user_profile_by_uid(UID)
                self.assertEqual(result.state, state)
                api._get.assert_awaited_once_with("https://api.bilibili.com/x/web-interface/card", {"mid": UID})
                if state == "failed":
                    self.assertIn("UID", result.detail)
                    self.assertIn("检查", result.detail)

    async def test_invalid_uid_never_becomes_a_search(self):
        for raw in ("UID:abc123", "UID:-123", "UID:1.2", "UID:1 UID:2", "0", "9" * 50):
            with self.subTest(raw=raw):
                api = BiliApi(client=object())
                api._get = AsyncMock()
                api.search_user_profile = AsyncMock()
                result = await api.resolve_user_profile(raw)
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.detail, UID_INVALID_MESSAGE)
                api._get.assert_not_awaited()
                api.search_user_profile.assert_not_awaited()

    async def test_uid_timeout_remains_unknown(self):
        api = BiliApi(client=object())
        api._get = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
        result = await api.resolve_user_profile(str(UID))
        self.assertEqual(result.state, "inaccessible")
        self.assertNotIn("未查到", result.detail)


class JoinRequestRoutingSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for attribute, filename in (
            ("DATABASE_FILE", "gatekeeper.sqlite3"),
            ("LEGACY_JOIN_REQUEST_FILE", "absent.json"),
            ("LEGACY_LEAVE_RECORDS_PATH", "absent-leaves"),
            ("VERY_LEGACY_LEAVE_RECORDS_PATH", "absent-old-leaves"),
        ):
            stack.enter_context(patch.object(database, attribute, root / filename))
        database.reset_database_state()
        self.addCleanup(database.reset_database_state)
        reset_wbi_key_cache()
        self.addCleanup(reset_wbi_key_cache)
        stack.enter_context(patch.object(manager, "_decision_service", DecisionService(decision_adapter.SqliteDecisionStorage())))
        stack.enter_context(patch.object(manager, "_notify_admins", new=AsyncMock()))

        async def storage(function, *args, **kwargs):
            return function(*args, **kwargs)

        stack.enter_context(patch.object(manager, "run_storage", new=storage))
        stack.enter_context(patch.object(decision_adapter, "run_storage", new=storage))
        self.group = GroupConfig(
            group_id="90001", required_bili_level=2, required_qq_level=4,
            reject_reasons={"bili_account": "管理员自定义账号提示"},
            review_stages=[ReviewStageConfig(
                index=1, conditions=["bili_account", "bili_level", "qq_level"],
                on_true=["approve"], on_false=["reject"], on_unknown=["ignore"],
                on_false_reason="阶段自定义拒绝提示",
            )],
        )
        stack.enter_context(patch.object(ConfigManager, "get_group_config", return_value=self.group))
        stack.enter_context(patch.object(ConfigManager, "get_bili_cookie", return_value="fixture-cookie"))
        self.http_calls = []
        self.uid_code = 0

        def transport(request):
            self.http_calls.append(request)
            if request.url.path.endswith("/nav"):
                return httpx.Response(200, json={"data": {}})
            if request.url.path.endswith("/card"):
                mid = int(request.url.params["mid"])
                payload = {"code": -404}
                if self.uid_code:
                    payload = {"code": self.uid_code}
                elif mid == UID:
                    payload = {"code": 0, "data": {"card": {"mid": str(mid), "name": NAME, "level_info": {"current_level": 6}}}}
                return httpx.Response(200, json=payload)
            if request.url.path.endswith("/search/type"):
                keyword = request.url.params["keyword"]
                users = [{"mid": UID, "uname": NAME, "level": 6}]
                if keyword != NAME:
                    users = [{"mid": UID, "uname": "Fuzzy_Other", "level": 6}]
                return httpx.Response(200, json={"code": 0, "data": {"numResults": 1, "result": users}})
            raise AssertionError("Unexpected endpoint: " + request.url.path)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.addAsyncCleanup(self.client.aclose)

        async def run_api(cookie, operation):
            return await operation(BiliApi(cookie, client=self.client))

        stack.enter_context(patch.object(manager._bili_requests, "run", new=run_api))

    async def submit(self, raw, flag):
        self.http_calls.clear()
        bot = SimpleNamespace(
            self_id="90000", get_stranger_info=AsyncMock(return_value={"level": 32}),
            set_group_add_request=AsyncMock(),
        )
        event = SimpleNamespace(flag=flag, sub_type="add", group_id=90001, user_id=10001, comment=raw)
        await manager.handle_group_request(bot, event)
        return bot, Records.get_all_user_audits("10001")[-1]

    async def test_valid_inputs_reach_approval_and_persist_an_applied_audit(self):
        for index, (raw, uid_route) in enumerate((
            (str(UID), True), ("UID:123456", True),
            ("问题：B站账号\n答案：B站UID：１２３，４５６", True),
            ("\u200b123456\ufeff", True), (NAME, False),
        )):
            with self.subTest(raw=raw):
                bot, audit = await self.submit(raw, "valid-" + str(index))
                bot.set_group_add_request.assert_awaited_once()
                self.assertTrue(bot.set_group_add_request.await_args.kwargs["approve"])
                self.assertEqual((audit.result, audit.action_status, audit.bili_uid), ("approved", "applied", UID))
                self.assertEqual(audit.condition_states, {"bili_account": "true", "bili_level": "true", "qq_level": "true"})
                paths = [request.url.path for request in self.http_calls]
                if uid_route:
                    self.assertEqual(paths, ["/x/web-interface/card"])
                else:
                    self.assertTrue(any(path.endswith("/search/type") for path in paths))
                    self.assertFalse(any(path.endswith("/card") for path in paths))

    async def test_wrong_uid_and_nickname_feedback_survives_custom_stage_text(self):
        for index, (raw, expected) in enumerate((("UID:999999999999", "UID"), ("Wrong_Name123", "昵称"))):
            with self.subTest(raw=raw):
                bot, audit = await self.submit(raw, "wrong-" + str(index))
                call = bot.set_group_add_request.await_args.kwargs
                self.assertFalse(call["approve"])
                self.assertIn(expected, call["reason"])
                self.assertIn("检查", call["reason"])
                self.assertIn("请不要填写B站昵称或UID以外的内容", call["reason"])
                self.assertIn("阶段自定义拒绝提示", call["reason"])
                self.assertTrue(call["reason"].startswith("来自BOT:"))
                self.assertEqual((audit.result, audit.action_status), ("rejected", "applied"))
                self.assertIn("检查", json.dumps(audit.reasons, ensure_ascii=False))
                paths = [request.url.path for request in self.http_calls]
                self.assertEqual(any(path.endswith("/card") for path in paths), expected == "UID")
                self.assertEqual(any(path.endswith("/search/type") for path in paths), expected == "昵称")

    async def test_api_failure_is_not_misreported_as_an_unknown_uid(self):
        self.uid_code = -352
        bot, audit = await self.submit(str(UID), "api-unavailable")
        bot.set_group_add_request.assert_not_awaited()
        self.assertEqual(audit.result, "ignored")
        self.assertEqual(audit.condition_states["bili_account"], "unknown")

    async def test_legacy_nickname_only_text_is_replaced_for_uid_failure(self):
        self.group.reject_reasons["bili_account"] = LEGACY_NICKNAME_NOT_FOUND_MESSAGE
        self.group.review_stages[0].on_false_reason = LEGACY_NICKNAME_NOT_FOUND_MESSAGE
        bot, audit = await self.submit("UID:999999999999", "legacy-feedback")
        reason = bot.set_group_add_request.await_args.kwargs["reason"]
        self.assertIn("未查到你填写的B站UID", reason)
        self.assertIn("检查", reason)
        self.assertIn("请不要填写B站昵称或UID以外的内容", reason)
        self.assertNotIn("未搜索到你填写的B站昵称", reason)
        self.assertNotIn(LEGACY_NICKNAME_NOT_FOUND_MESSAGE, audit.condition_reasons["bili_account"])

    async def test_check_command_keeps_uid_value_after_a_space_and_never_writes_audit(self):
        finish = AsyncMock(side_effect=FinishedException)
        with (
            patch.object(manager, "_is_private_admin_event", return_value=True),
            patch.object(manager.check_bili, "finish", new=finish),
        ):
            with self.assertRaises(FinishedException):
                await manager.handle_check_bili(SimpleNamespace(user_id=10001), arg="90001 UID: 123456")
        self.assertIn("B站 UID：123456", finish.await_args.args[0])
        self.assertEqual([r.url.path for r in self.http_calls], ["/x/web-interface/card"])
        self.assertEqual(Records.get_all_user_audits("10001"), [])


if __name__ == "__main__":
    unittest.main()
