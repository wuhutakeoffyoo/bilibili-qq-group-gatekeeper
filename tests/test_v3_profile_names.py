"""Canonical profile names through HTTP routing, SQL audit and administrator replies."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.bilibili.pacing import OBSERVATIONS
from app.commands import CommandService
from app.config import ConfigManager
from app.database import legacy
from app.database.records import JoinRequestRecordManager as Records
from app.service import GatekeeperService
from src.gatekeeper.config import GroupConfig, ReviewStageConfig

UID, NAME, QQ = 456, '接口返回的昵称', '200'


class CaptureBot:
    self_id = '99'

    def __init__(self):
        self.actions, self.messages = [], []

    async def get_stranger_info(self, **kwargs):
        return {'level': 32}

    async def set_group_add_request(self, **kwargs):
        self.actions.append(kwargs)

    async def send_private_msg(self, **kwargs):
        self.messages.append(kwargs)


class ProfileNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.settings = SimpleNamespace(app_root=Path(self.temp.name), superusers=['1'], command_start=['/', ''])
        ConfigManager.reset_state()
        ConfigManager.initialize(self.settings)
        ConfigManager.set_bili_cookie('SESSDATA=profile-fixture')
        self.group = GroupConfig(group_id='10', required_bili_level=2, required_qq_level=32,
            target_uids=[999], target_medal_uids=[999], required_medal_level=1,
            review_stages=[
                ReviewStageConfig(index=1, name='筛选组A', conditions=[
                    'bili_account', 'bili_level', 'qq_level', 'no_leave_record', 'follow'],
                    on_true=['approve'], on_false=['reject'], on_unknown=['next']),
                ReviewStageConfig(index=2, name='筛选组B', conditions=['medal'], mode='any_pass',
                    on_true=['approve'], on_false=['reject'], on_unknown=['ignore', 'notify_admin'])])
        ConfigManager.set_group_config(self.group)
        self.requests = []
        self.card_mode = 'valid'
        self.mock_http = patch('app.bilibili.runtime.create_bili_http_client',
            side_effect=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(self.wire), trust_env=False))
        self.mock_http.start()
        self.pacing = patch('app.bilibili.client.HTTP_LIMITER.acquire', new=AsyncMock())
        self.pacing.start()
        OBSERVATIONS.clear()
        self.bot = CaptureBot()
        self.service = GatekeeperService(settings=self.settings, client_provider=lambda: self.bot)
        self.commands = CommandService(self.service)

    async def asyncTearDown(self):
        await self.service.close()
        await OBSERVATIONS.close()
        self.pacing.stop()
        self.mock_http.stop()
        ConfigManager.reset_state()
        legacy.reset_database_state()
        self.environment.stop()
        self.temp.cleanup()

    def wire(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith('/card'):
            self.assertEqual(request.url.params['mid'], str(UID))
            if self.card_mode == 'unavailable':
                return httpx.Response(200, json={'code': -412})
            card = {'mid': str(UID if self.card_mode != 'wrong_uid' else UID + 1),
                    'name': NAME, 'level_info': {'current_level': 5}}
            if self.card_mode == 'missing_name':
                card.pop('name')
            return httpx.Response(200, json={'code': 0, 'data': {'card': card}})
        if path.endswith('/search/type'):
            self.assertEqual(request.url.params['keyword'], 'summername')
            return httpx.Response(200, json={'code': 0, 'data': {'numResults': 1,
                'result': [{'mid': UID, 'uname': 'SummerName', 'level': 5}]}})
        if path.endswith('/nav'):
            return httpx.Response(200, json={'code': 0, 'data': {'mid': 888, 'isLogin': True}})
        if '/relation/' in path:
            return httpx.Response(200, json={'code': 22115, 'message': '用户已设置隐私'})
        if path.endswith('/MedalWall'):
            return httpx.Response(200, json={'code': 0, 'data': {'close_space_medal': 1}})
        raise AssertionError('Unexpected HTTP request: ' + path)

    async def request(self, answer, flag='name-regression'):
        await self.service.handle_group_request(self.bot, {'post_type': 'request', 'request_type': 'group',
            'sub_type': 'add', 'self_id': 99, 'group_id': 10, 'user_id': int(QQ), 'flag': flag, 'comment': answer})

    async def report(self):
        return await self.commands.dispatch('1', {'message_type': 'private', 'user_id': 1, 'message': '/检查qq ' + QQ})

    def old_audit(self):
        Records.add_audit(group_id='10', qq=QQ, bili_uid=UID, bili_name=f'UID:{UID}', result='ignored',
                          reasons=['无法检索目标主播粉丝灯牌'], condition_states={'follow': 'unknown', 'medal': 'unknown'})

    async def test_private_follow_and_medal_still_ignore_but_store_and_display_resolved_name(self):
        await self.request(f'UID:{UID}')
        binding, summary, audits = Records.get_user_report(QQ, 5)
        self.assertEqual(binding.latest_bili_name, NAME)
        self.assertEqual((summary.approved_count, summary.rejected_count, summary.ignored_count), (0, 0, 1))
        self.assertEqual((audits[0].result, audits[0].bili_name, audits[0].bili_uid), ('ignored', NAME, UID))
        self.assertEqual(audits[0].condition_states['bili_account'], 'true')
        for condition in ('follow', 'medal'):
            self.assertEqual(audits[0].condition_states[condition], 'unknown')
        self.assertFalse(self.bot.actions)
        self.assertIsNone(Records.get_bili_binding(UID))
        self.assertTrue(any('B站昵称：' + NAME in message['message'] for message in self.bot.messages))
        request_count = len(self.requests)
        reply = await self.report()
        self.assertIn('最近绑定的 B站昵称：' + NAME, reply)
        self.assertIn('B站昵称：' + NAME, reply)
        self.assertNotIn('B站昵称：UID:', reply)
        self.assertEqual(len(self.requests), request_count)
        self.assertFalse(any('search/type' in request.url.path for request in self.requests))

    async def test_approved_identity_uses_card_name_in_ownership_binding(self):
        self.group.review_stages = [ReviewStageConfig(index=1, conditions=['bili_account'], on_true=['approve'])]
        ConfigManager.set_group_config(self.group)
        await self.request(str(UID))
        self.assertTrue(self.bot.actions[0]['approve'])
        self.assertEqual(Records.get_bili_binding(UID).bili_names, [NAME])
        self.assertEqual([request.url.path for request in self.requests], ['/x/web-interface/card'])

    async def test_old_uid_placeholder_is_enriched_only_in_reply_without_sql_writes(self):
        self.old_audit()
        before = Records.get_user_report(QQ, 5)
        reply = await self.report()
        self.assertIn('最近绑定的 B站昵称：' + NAME, reply)
        self.assertIn('B站昵称：' + NAME, reply)
        self.assertIn(f'申请填写：UID:{UID}', reply)
        self.assertIn('结果：忽略', reply)
        self.assertEqual(Records.get_user_report(QQ, 5), before)
        self.assertFalse(self.bot.actions)
        self.assertEqual([request.url.path for request in self.requests], ['/x/web-interface/card'])

    async def test_unavailable_old_profile_is_not_mislabeled_as_a_nickname(self):
        self.old_audit()
        self.card_mode = 'unavailable'
        before = Records.get_user_report(QQ, 5)
        reply = await self.report()
        self.assertIn('最近绑定的 B站昵称：未记录', reply)
        self.assertNotIn('B站昵称：UID:', reply)
        self.assertNotIn(NAME, reply)
        self.assertEqual(Records.get_user_report(QQ, 5), before)

    async def test_name_with_uid_text_for_another_number_is_not_a_placeholder(self):
        Records.add_audit(group_id='10', qq=QQ, bili_uid=UID, bili_name='UID:777', result='ignored', reasons=[])
        reply = await self.report()
        self.assertIn('B站昵称：UID:777', reply)
        self.assertEqual(self.requests, [])

    async def test_mismatched_uid_cannot_supply_historical_display_name(self):
        self.old_audit()
        self.card_mode = 'wrong_uid'
        reply = await self.report()
        self.assertNotIn(NAME, reply)
        self.assertNotIn('B站昵称：UID:', reply)
        self.assertEqual(len(self.requests), 1)

    async def test_nickname_search_uses_the_api_canonical_case(self):
        result = await self.service.simulate('10', 'summername')
        self.assertEqual((result['bili_uid'], result['bili_name']), (UID, 'SummerName'))
        self.assertIn('B站昵称：SummerName', result['text'])
        self.assertFalse(any(request.url.path.endswith('/card') for request in self.requests))
        self.assertEqual(Records.get_user_report(QQ, 5)[1].attempt_count, 0)

    async def test_missing_name_does_not_change_uid_qualification_or_echo_the_uid_as_name(self):
        self.card_mode = 'missing_name'
        for answer in (str(UID), 'B站UID：４５６'):
            result = await self.service.simulate('10', answer)
            self.assertEqual(result['condition_states']['bili_account'], 'true')
            self.assertEqual(result['bili_uid'], UID)
            self.assertEqual(result['bili_name'], '')
            self.assertIn('B站昵称：未获取到昵称', result['text'])
        self.assertFalse(any('search/type' in request.url.path for request in self.requests))
