"""Ownership capabilities and patrol action acceptance against real SQLite."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.bilibili.proof import ProofClient, ProofObservation
from app.config import ConfigManager
from app.database import legacy
from app.database.records import JoinRequestRecordManager as Records
from app.database.repository import RuntimeStateRepository as State
from app.onebot.events import OneBotEvent
from app.ownership import OwnershipService
from app.runtime import Runtime
from app.gatekeeper.decisions import RequestEvidence
from app.patrol import PatrolService
from app.service import GatekeeperService, JoinRequestContext
from app.scheduling import CronSchedule
from app.web.verify_h5 import install_verify_routes
from src.gatekeeper.config import GroupConfig, ReviewStageConfig
from src.gatekeeper.models import CheckResult


class BiliFixture:
    def __init__(self):
        self.answers = []

    async def run(self, cookie, operation, **kwargs):
        return await operation(self)

    async def resolve_user_profile(self, answer):
        self.answers.append(answer)
        return CheckResult("passed", "账号存在", matched_target_uid=int(str(answer).split(':')[-1]), matched_level=6)

    def common_negative_is_definitive(self, *args):
        return True

    async def close(self):
        pass


class QQFixture:
    self_id = "99"
    connected = True

    def __init__(self):
        self.members = {"10": [{"user_id": 99, "role": "owner"}, {"user_id": 200, "role": "member"}], "20": []}
        self.actions = []
        self.kicks = []
        self.messages = []
        self.group_messages = []
        self.ambiguous = False
        self.qq_level = 42

    async def get_stranger_info(self, **kwargs):
        return {"level": self.qq_level}

    async def get_group_list(self):
        return [{"group_id": int(group)} for group in self.members]

    async def get_group_member_list(self, group_id):
        return copy.deepcopy(self.members[str(group_id)])

    async def set_group_add_request(self, **params):
        self.actions.append(params)

    async def send_private_msg(self, **params):
        self.messages.append(params)

    async def send_group_msg(self, **params):
        key = f"{params['group_id']}:200:456:remind"
        if State.get("patrol_actions", key, {}).get("state") != "pending":
            raise AssertionError("group reminder ran before SQLite pending intent")
        self.group_messages.append(params)

    async def set_group_kick(self, **params):
        key = f"{params['group_id']}:{params['user_id']}:456:kick"
        if State.get("patrol_actions", key, {}).get("state") != "pending":
            raise AssertionError("transport ran before SQLite pending intent")
        self.kicks.append(params)
        if self.ambiguous:
            raise TimeoutError("lost acknowledgement")


class OwnershipPatrolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.settings = SimpleNamespace(app_root=Path(self.temp.name), superusers=["1"])
        ConfigManager.reset_state()
        ConfigManager.initialize(self.settings)
        ConfigManager.set_bili_cookie("SESSDATA=test")
        self.group = GroupConfig(group_id="10", target_uids=[11], required_qq_level=32,
            review_stages=[ReviewStageConfig(index=1, conditions=["qq_level"], on_true=["approve"], on_false=["reject"])],
            verification={"mode": "dm", "receiver_uid": "99", "public_url": "https://verify.invalid", "ttl_seconds": 120},
            patrol={"enabled": True, "mode": "kick", "grace_seconds": 60, "when": "follow"})
        ConfigManager.set_group_config(self.group)
        self.now = [1000.0]
        self.bili = BiliFixture()
        self.qq = QQFixture()
        self.service = GatekeeperService(settings=self.settings, bili_requests=self.bili)
        self.runtime = SimpleNamespace(client=self.qq, service=self.service, notify=AsyncMock())
        self.proof_result = ProofObservation("verified", "匹配", "message-1")
        self.proof_calls = []
        case = self

        class ProofFixture:
            def __init__(self, *args, **kwargs):
                pass

            async def observe(self, *args):
                case.proof_calls.append(args)
                return case.proof_result

            async def close(self):
                pass

        self.ownership = OwnershipService(api_factory=ProofFixture, clock=lambda: self.now[0])
        self.service.ownership = self.ownership
        self.observations = []
        self.default_observation = CheckResult("failed", "未关注本群指定主播")
        self.medal_observation = CheckResult("failed", "没有粉丝牌", matched_level=0)
        self.on_observe = None
        self.scopes = []

        class PatrolApiFixture:
            def __init__(self, cookie, **kwargs):
                case.scopes.append(kwargs.get("cache_scope"))

            async def check_follow_any_status(self, uid, *args, **kwargs):
                if case.on_observe:
                    await case.on_observe(uid)
                return case.observations.pop(0) if case.observations else case.default_observation

            async def check_medal_any_status(self, uid, *args, **kwargs):
                return case.medal_observation

            async def close(self):
                pass

        self.patrol = PatrolService(self.runtime, api_factory=PatrolApiFixture, clock=lambda: self.now[0], rate=1000000)

    async def asyncTearDown(self):
        await self.service.close()
        ConfigManager.reset_state()
        legacy.reset_database_state()
        self.environment.stop()
        self.temp.cleanup()

    def context(self, flag="first", qq="200", uid=456):
        return JoinRequestContext("10", qq, ConfigManager.get_group_config("10"), bili_uid=uid,
                                  bot=self.qq, event=OneBotEvent({"flag": flag, "sub_type": "add"}))

    @staticmethod
    def capability(text):
        link = re.search(r"https://[^。\s]+", text).group()
        values = parse_qs(urlsplit(link).fragment)
        return values["id"][0], values["secret"][0]

    def approve_fixture(self, *, group="10", qq="200", uid=456):
        audit = Records.begin_pending_decision(group_id=group, qq=qq, bili_uid=uid, bili_name="name",
                                               result="approved", reasons=["passed"], request_key=f"{group}:{qq}:{uid}")
        self.assertTrue(Records.finalize_pending_decision(audit.id, applied=True))

    async def qr_gateway(self, *, evidence_state='unhandled', wrong_flag=False):
        group = ConfigManager.get_group_config('10').model_copy(deep=True)
        group.verification = {'mode': 'qr', 'public_url': 'https://verify.invalid', 'ttl_seconds': 120}
        ConfigManager.set_group_config(group)
        class QrFixture:
            async def start(self, *args):
                return {'session_id': 'fixture-session', 'url': 'https://passport.bilibili.com/fixture', 'expires_at': 1180}
            async def poll(self, *args):
                return {'state': 'verified'}
            async def close(self):
                pass
        self.ownership.qr = QrFixture()
        runtime = Runtime(settings=self.settings, service=self.service, clients={'qq': self.qq},
                          ownership=self.ownership, background_enabled=False)
        async def inspect(metadata):
            values = {key: metadata[key] for key in ('bot_id', 'group_id', 'qq', 'sub_type', 'flag', 'expected_result')}
            if wrong_flag:
                values['flag'] = 'another-request'
            return RequestEvidence(state=evidence_state, **values)
        runtime.request_inspector = inspect
        self.ownership.resume = runtime.resume_ownership
        event = {'post_type': 'request', 'request_type': 'group', 'sub_type': 'add',
                 'self_id': 99, 'group_id': 10, 'user_id': 200, 'comment': '456', 'flag': 'qr-held'}
        await self.service.handle_group_request(self.qq, event)
        self.assertEqual(self.qq.actions, [])
        self.assertEqual(len(self.qq.messages), 1)
        identifier, secret = self.capability(self.qq.messages[0]['message'])
        app = FastAPI()
        install_verify_routes(app, runtime)
        return app, {'challenge_id': identifier, 'secret': secret}, event

    async def test_qr_h5_resumes_only_exact_unhandled_request_and_preserves_operator_credentials(self):
        app, cap, event = await self.qr_gateway()
        credentials = ConfigManager.get_snapshot(include_secrets=True)
        await self.service.handle_group_request(self.qq, event)
        self.assertEqual(len(self.qq.messages), 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            state = await client.post('/verify/state', json=cap)
            self.assertEqual(state.json()['mode'], 'qr')
            generated = await client.post('/verify/qr/start', json=cap)
            self.assertTrue(generated.json()['image'].startswith('data:image/svg+xml;base64,'))
            response = await client.post('/verify/qr/poll', json={**cap, 'qr_session_id': generated.json()['session_id']})
            self.assertEqual(response.json()['resume']['state'], 'approved')
            replay = await client.post('/verify/qr/poll', json={**cap, 'qr_session_id': generated.json()['session_id']})
            self.assertEqual(replay.json()['state'], 'expired')
        self.assertEqual(self.qq.actions, [{'flag': 'qr-held', 'sub_type': 'add', 'approve': True}])
        self.assertEqual(Records.get_bili_binding(456).owner_qq, '200')
        self.assertEqual(ConfigManager.get_snapshot(include_secrets=True), credentials)

    async def test_qr_h5_does_not_bypass_changed_qualification(self):
        app, cap, _ = await self.qr_gateway()
        self.qq.qq_level = 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            session = (await client.post('/verify/qr/start', json=cap)).json()
            result = (await client.post('/verify/qr/poll', json={**cap, 'qr_session_id': session['session_id']})).json()
        self.assertEqual(result['resume']['state'], 'rejected')
        self.assertFalse(self.qq.actions[-1]['approve'])
        self.assertIn('32', self.qq.actions[-1]['reason'])
        self.assertIsNone(Records.get_bili_binding(456))

    async def test_qr_h5_unknown_original_request_retains_proof_for_new_request(self):
        app, cap, event = await self.qr_gateway(evidence_state='unknown')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            session = (await client.post('/verify/qr/start', json=cap)).json()
            result = (await client.post('/verify/qr/poll', json={**cap, 'qr_session_id': session['session_id']})).json()
        self.assertEqual(result['resume']['state'], 'verified')
        self.assertEqual(self.qq.actions, [])
        self.assertEqual(State.get('ownership', cap['challenge_id'])['state'], 'verified')
        await self.service.handle_group_request(self.qq, {**event, 'flag': 'new-request'})
        self.assertTrue(self.qq.actions[-1]['approve'])

    async def test_qr_h5_wrong_request_evidence_never_approves(self):
        app, cap, _ = await self.qr_gateway(wrong_flag=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            session = (await client.post('/verify/qr/start', json=cap)).json()
            result = (await client.post('/verify/qr/poll', json={**cap, 'qr_session_id': session['session_id']})).json()
        self.assertEqual(result['resume']['state'], 'verified')
        self.assertEqual(self.qq.actions, [])

    async def test_challenge_bound_to_issued_capability_expires_rotates_and_consumes_once(self):
        identifier, secret = self.capability(await self.ownership.before_decision(self.context()))
        stored = State.get("ownership", identifier)
        self.assertNotIn(secret, json.dumps(stored))
        with self.assertRaises(ValueError):
            await self.ownership.challenge(identifier, "wrong-secret-0123456789")
        verified = await self.ownership.challenge(identifier, secret, check=True)
        self.assertEqual(verified["state"], "verified")
        self.assertIsNone(await self.ownership.before_decision(self.context(flag="second")))
        self.assertIsNone(await self.ownership.before_decision(self.context(flag="second")))
        self.assertEqual((await self.ownership.challenge(identifier, secret))["state"], "expired")
        replacement = self.capability(await self.ownership.before_decision(self.context(flag="third")))
        self.assertNotEqual(replacement[1], secret)
        with self.assertRaises(ValueError):
            await self.ownership.challenge(identifier, secret)
        self.now[0] += 121
        self.assertEqual((await self.ownership.challenge(*replacement, check=True))["state"], "expired")
        self.assertEqual(len(self.proof_calls), 1)

    async def test_duplicate_original_request_does_not_invalidate_delivered_link(self):
        context = self.context()
        identifier, secret = self.capability(await self.ownership.before_decision(context))
        await self.ownership.before_decision(context)
        state = await self.ownership.challenge(identifier, secret)
        self.assertEqual(state["state"], "pending")

    async def test_public_routes_cannot_choose_qq_uid_or_issue_arbitrary_grant(self):
        app = FastAPI()
        install_verify_routes(app, SimpleNamespace(ownership=self.ownership))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://verify.invalid") as client:
            arbitrary = await client.post("/verify/check", json={"qq": "200", "uid": 456})
            absent = await client.post("/verify/check", json={"challenge_id": "a" * 32, "secret": "b" * 24})
            self.assertEqual(arbitrary.status_code, 400)
            self.assertEqual(absent.status_code, 400)
        self.assertEqual(State.list_namespace("ownership"), {})

    async def test_qualification_runs_before_consuming_verified_proof_and_identity_is_resolved_for_qq_only_rule(self):
        event = {"sub_type": "add", "group_id": 10, "user_id": 200, "comment": "456", "flag": "initial"}
        await self.service.handle_group_request(self.qq, event)
        self.assertEqual(self.bili.answers, ["456"])
        identifier, secret = self.capability(self.qq.actions[-1]["reason"])
        await self.ownership.challenge(identifier, secret, check=True)
        self.qq.qq_level = 1
        await self.service.handle_group_request(self.qq, {**event, "flag": "failed-rule"})
        self.assertFalse(self.qq.actions[-1]["approve"])
        self.assertEqual(State.get("ownership", identifier)["state"], "verified")
        self.qq.qq_level = 42
        await self.service.handle_group_request(self.qq, {**event, "flag": "qualified"})
        self.assertTrue(self.qq.actions[-1]["approve"])
        self.assertEqual(State.get("ownership", identifier)["state"], "consumed")
        self.assertEqual(Records.get_bili_binding(456).owner_qq, "200")

    async def test_existing_uncertain_approval_does_not_consume_a_verified_ownership_grant(self):
        identifier, secret = self.capability(await self.ownership.before_decision(self.context()))
        await self.ownership.challenge(identifier, secret, check=True)
        Records.begin_pending_decision(group_id="20", qq="200", bili_uid=456,
                                       result="approved", reasons=["uncertain prior action"])
        await self.service.handle_group_request(self.qq, {"sub_type": "add", "group_id": 10,
            "user_id": 200, "comment": "456", "flag": "new-while-pending"})
        self.assertEqual(self.qq.actions, [])
        self.assertEqual(State.get("ownership", identifier)["state"], "verified")

    async def test_real_proof_adapter_requires_exact_sender_receiver_nonce_and_time(self):
        valid = dict(msg_type=1, sender_uid=456, receiver_id=99, timestamp=1001,
                     content=json.dumps({"content": "GK-nonce"}), msg_key="proof")
        changes = [({"sender_uid": 457}, "wrong author"), ({"receiver_id": 98}, "wrong receiver"),
                   ({"timestamp": 999}, "before challenge"), ({"timestamp": 1121}, "after expiry"),
                   ({"content": json.dumps({"content": "prefix GK-nonce"})}, "extra text"),
                   ({"msg_type": 2}, "wrong message type")]
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        api = ProofClient("cookie", client=client)
        try:
            with patch("app.bilibili.proof.time.time", return_value=1100), patch.object(api, "_get_wbi_keys", AsyncMock(return_value=("a", "b"))):
                for change, label in [({}, "exact match"), *changes]:
                    with self.subTest(case=label):
                        responses = [{"code": 0, "data": {"isLogin": True, "mid": 99}},
                                     {"code": 0, "data": {"messages": [{**valid, **change}]}}]
                        with patch.object(api, "_get", AsyncMock(side_effect=responses)):
                            observation = await api.observe({"mode": "dm", "receiver_uid": "99"}, 456, "GK-nonce", 1000, 1120)
                        self.assertEqual(observation.state, "verified" if not change else "waiting")
        finally:
            await client.aclose()

    async def test_unknown_patrol_never_schedules_or_executes_kick(self):
        self.approve_fixture()
        self.default_observation = CheckResult("inaccessible", "无法读取关注状态")
        report = await self.patrol.run_group("10")
        self.now[0] += 120
        result = await self.patrol.act(report["id"], "kick")
        self.assertEqual(report["items"][0]["state"], "unknown")
        self.assertEqual(result["results"], [])
        self.assertEqual(self.qq.kicks, [])

    async def test_patrol_qualification_or_rule_does_not_strengthen_into_and(self):
        self.approve_fixture()
        current = ConfigManager.get_group_config("10").model_copy(deep=True)
        current.target_medal_uids = [33]
        current.patrol["when"] = "follow || medal >= 1"
        ConfigManager.set_group_config(current)
        self.default_observation = CheckResult("passed", "已关注")
        report = await self.patrol.run_group("10")
        self.assertEqual(report["items"][0]["state"], "passed")
        self.assertEqual((await self.patrol.act(report["id"], "kick"))["results"], [])
        self.assertEqual(self.qq.kicks, [])

    async def test_missing_or_constant_patrol_expression_cannot_authorize_a_kick(self):
        self.approve_fixture()
        current = ConfigManager.get_group_config("10").model_copy(deep=True)
        current.patrol.pop("when")
        ConfigManager.set_group_config(current)
        report = await self.patrol.run_group("10")
        self.assertEqual(report["items"][0]["state"], "unknown")
        for expression in ("True", "False", "qq_level >= 1"):
            with self.subTest(expression=expression):
                current.patrol["when"] = expression
                version = ConfigManager.get_version()
                with self.assertRaises(ValueError):
                    ConfigManager.set_group_config(current)
                self.assertEqual(ConfigManager.get_version(), version)
        self.assertEqual(self.qq.kicks, [])

    async def test_group_at_reminder_uses_durable_intent_and_does_not_repeat(self):
        self.approve_fixture()
        report = await self.patrol.run_group("10")
        result = await self.patrol.act(report["id"], "remind")
        self.assertEqual(result["results"][0]["state"], "applied")
        message = self.qq.group_messages[0]
        self.assertEqual(message["group_id"], 10)
        self.assertEqual(message["message"][0], {"type": "at", "data": {"qq": "200"}})
        self.assertEqual(State.get("patrol_actions", "10:200:456:remind")["state"], "applied")
        await self.patrol.act(report["id"], "remind")
        self.assertEqual(len(self.qq.group_messages), 1)

    async def test_patrol_requires_grace_fresh_negative_and_persists_intent_before_transport(self):
        self.approve_fixture()
        report = await self.patrol.run_group("10")
        early = await self.patrol.act(report["id"], "kick")
        self.assertEqual(early["results"][0]["state"], "waiting")
        self.now[0] += 61
        self.default_observation = CheckResult("inaccessible", "临时无法读取")
        uncertain = await self.patrol.act(report["id"], "kick")
        self.assertEqual(uncertain["results"][0]["state"], "skipped")
        self.assertEqual(self.qq.kicks, [])
        self.default_observation = CheckResult("failed", "确认未关注")
        report = await self.patrol.run_group("10")
        self.now[0] += 61
        self.qq.ambiguous = True
        pending = await self.patrol.act(report["id"], "kick")
        self.assertEqual(pending["results"][0]["state"], "pending")
        self.assertEqual(State.get("patrol_actions", "10:200:456:kick")["state"], "pending")
        await self.patrol.act(report["id"], "kick")
        self.assertEqual(len(self.qq.kicks), 1)
        self.assertEqual(len(set(self.scopes)), len(self.scopes))

    async def test_changed_role_binding_or_pending_approval_prevents_patrol_action(self):
        self.approve_fixture()
        report = await self.patrol.run_group("10")
        self.now[0] += 61
        self.qq.members["10"][1]["role"] = "admin"
        self.assertEqual((await self.patrol.act(report["id"], "kick"))["results"][0]["state"], "skipped")
        self.qq.members["10"][1]["role"] = "member"
        pending = Records.begin_pending_decision(group_id="20", qq="200", bili_uid=456, result="approved", reasons=["pending"])
        self.assertEqual((await self.patrol.act(report["id"], "kick"))["results"][0]["state"], "skipped")
        Records.finalize_pending_decision(pending.id, applied=False)
        Records.clear_identity_binding("200")
        self.assertEqual((await self.patrol.act(report["id"], "kick"))["results"][0]["state"], "skipped")
        self.assertEqual(self.qq.kicks, [])

    async def test_old_negative_report_cannot_bypass_a_grace_reset_after_recovery(self):
        self.approve_fixture()
        report = await self.patrol.run_group("10")
        self.now[0] += 30
        self.default_observation = CheckResult("passed", "已恢复关注")
        await self.patrol.run_group("10")
        self.assertIsNone(State.get("patrol_grace", "10:200:456"))
        self.now[0] += 31
        self.default_observation = CheckResult("failed", "再次取消关注")
        await self.patrol.act(report["id"], "kick")
        self.assertEqual(self.qq.kicks, [])

    async def test_policy_changed_during_remote_recheck_prevents_kick(self):
        self.approve_fixture()
        report = await self.patrol.run_group("10")
        self.now[0] += 61
        async def change_policy(uid):
            current = ConfigManager.get_group_config("10").model_copy(deep=True)
            current.target_uids = [22]
            ConfigManager.set_group_config(current)
        self.on_observe = change_policy
        await self.patrol.act(report["id"], "kick")
        self.assertEqual(self.qq.kicks, [])

    async def test_other_groups_identity_is_not_a_negative_for_this_groups_approved_identity(self):
        self.approve_fixture(uid=456)
        self.approve_fixture(group="20", uid=789)
        self.observations = [CheckResult("passed", "本群账号已关注"), CheckResult("failed", "其他群账号未关注")]
        report = await self.patrol.run_group("10")
        self.assertEqual([item["uid"] for item in report["items"]], [456])

    async def test_leave_retains_binding_for_other_actual_group_presence_or_pending(self):
        self.approve_fixture()
        current = ConfigManager.get_group_config("10").model_copy(deep=True)
        current.release_binding_on_leave = True
        ConfigManager.set_group_config(current)
        self.qq.members["10"] = [{"user_id": 99, "role": "owner"}]
        self.qq.members["20"] = [{"user_id": 200, "role": "member"}]
        self.assertEqual((await self.patrol.on_group_decrease("10", "200"))["state"], "retained")
        self.qq.members["20"] = []
        pending = Records.begin_pending_decision(group_id="20", qq="200", bili_uid=456, result="approved", reasons=["pending"])
        self.assertEqual((await self.patrol.on_group_decrease("10", "200"))["state"], "retained")
        Records.finalize_pending_decision(pending.id, applied=False)
        self.assertEqual((await self.patrol.on_group_decrease("10", "200"))["state"], "released")
        self.assertIsNone(Records.get_bili_binding(456))

    async def test_malformed_membership_list_does_not_prove_absence_for_unbinding(self):
        self.approve_fixture()
        current = ConfigManager.get_group_config("10").model_copy(deep=True)
        current.release_binding_on_leave = True
        ConfigManager.set_group_config(current)
        self.qq.members["10"] = [{"unexpected_payload": 200}]
        self.qq.members["20"] = []
        self.assertEqual((await self.patrol.on_group_decrease("10", "200"))["state"], "retained")
        self.assertIsNotNone(Records.get_bili_binding(456))

    def test_weekly_cron_uses_named_zone_and_matches_only_scheduled_minute(self):
        schedule = CronSchedule("0 3 * * 0", "Asia/Shanghai")
        sunday_3am_shanghai = datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc).timestamp()
        self.assertTrue(schedule.matches(sunday_3am_shanghai))
        self.assertFalse(schedule.matches(sunday_3am_shanghai + 60))
        self.assertFalse(schedule.matches(sunday_3am_shanghai + 86400))
        self.assertFalse(CronSchedule("0 3 * * 0", "UTC").matches(sunday_3am_shanghai))
        self.assertTrue(CronSchedule("0 3 * * 7", "Asia/Shanghai").matches(sunday_3am_shanghai))


if __name__ == "__main__":
    unittest.main()
