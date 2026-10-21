"""Low-priority, opt-in membership patrol with durable action intents."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time

from app.bilibili.client import BiliApi
from app.bilibili.pacing import RequestLimiter
from app.config import ConfigManager
from app.database.async_storage import run_storage
from app.database.legacy import database_connection
from app.database.records import JoinRequestRecordManager as Records
from app.database.repository import RuntimeStateRepository as State
from app.scheduling import CronSchedule
from src.gatekeeper.config import parse_bool
from src.gatekeeper.decision_runtime import identity_decision_keys
from src.gatekeeper.decisions import DefiniteActionFailure
from src.gatekeeper.expressions import compile_expression
from src.gatekeeper.models import RuleConditionStatus

logger = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PatrolService:
    def __init__(self, runtime, config=ConfigManager, *, api_factory=None, clock=time.time, rate=.2):
        self.runtime, self.config = runtime, config
        self.api_factory, self.clock = api_factory or BiliApi, clock
        self.limiter = RequestLimiter(rate)
        self.running = set()
        self.jobs = set()

    @staticmethod
    def _policy(group):
        policy = dict(group.patrol or {})
        policy.setdefault('enabled', False)
        policy.setdefault('mode', 'report')
        if policy['mode'] not in {'report', 'remind', 'kick'}:
            raise ValueError('巡检 mode 只能为 report、remind 或 kick')
        if policy.get('cron'):
            CronSchedule(policy['cron'], policy.get('timezone', 'Asia/Shanghai'))
        if policy.get('remind_channel', 'group') not in {'group', 'private'}:
            raise ValueError('remind_channel 只能为 group 或 private')
        if policy.get('when'):
            expression = compile_expression(str(policy['when']))
            if not expression.conditions or set(expression.conditions) - {'follow', 'medal'}:
                raise ValueError('巡检 when 只允许关注、粉丝牌及其等级条件')
        return policy

    @staticmethod
    def _fingerprint(group):
        return _digest({'patrol': group.patrol, 'follow': group.target_uids,
                        'medals': group.target_medal_uids, 'level': group.required_medal_level})

    async def _members(self, group_id):
        client = self.runtime.client
        if client is None:
            raise ValueError('QQ连接不可用，未执行巡检动作')
        members = await client.get_group_member_list(group_id=int(group_id))
        if not isinstance(members, list) or len(members) > 20000:
            raise ValueError('未取得完整有效的群成员列表')
        if any(not isinstance(row, dict) or not str(row.get('user_id', '')).isdigit() for row in members):
            raise ValueError('群成员列表包含无法解析的成员，不能确认完整名单')
        identifiers = [str(int(row['user_id'])) for row in members]
        if len(set(identifiers)) != len(identifiers) or any(int(value) <= 0 for value in identifiers):
            raise ValueError('群成员列表含重复或无效账号，不能确认完整名单')
        # Require explicit roles; an absent role must never permit removal.
        return dict(zip(identifiers, members))

    async def _observe(self, group, uid):
        policy = self._policy(group)
        if not str(policy.get('when', '')).strip():
            return {'state': 'unknown', 'reasons': ['请先配置 patrol.when 巡检资格表达式，避免改变入群时的与/或条件']}
        expression = compile_expression(str(policy['when']))
        cookie = self.config.get_bili_cookie()
        if not cookie:
            return {'state': 'unknown', 'reasons': ['未配置B站凭证']}
        await self.limiter.acquire()
        # Different cache scope on every recheck: a kick cannot use the report's
        # cached observation. All wire requests still share the global limiter.
        api = self.api_factory(cookie, cache_scope='patrol:' + secrets.token_hex(8))
        checks = {}
        try:
            if 'follow' in expression.conditions and group.target_uids:
                coordinator = self.runtime.service.bili_requests
                checks['follow'] = await api.check_follow_any_status(uid, group.target_uids,
                    common_negative_is_definitive=coordinator.common_negative_is_definitive(cookie, group.target_uids))
            if 'medal' in expression.conditions and group.target_medal_uids:
                checks['medal'] = await api.check_medal_any_status(uid, group.target_medal_uids, group.required_medal_level)
        finally:
            await api.close()
        statuses = {name: RuleConditionStatus(
            {'passed': 'true', 'failed': 'false'}.get(check.state, 'unknown'), check.detail,
            observation=check.matched_level if name == 'medal' else None) for name, check in checks.items()}
        outcome = expression.evaluate(statuses)
        return {'state': {'true': 'passed', 'false': 'failed', 'unknown': 'unknown'}[outcome],
                'reasons': [] if outcome == 'true' else [expression.explain(statuses)]}

    @staticmethod
    def _group_uid(group_id, qq):
        # The global UID owner is not a group allocation: one QQ can have
        # separate accepted identities in separate groups. Use the latest
        # confirmed approval for THIS group, preserving legacy accepted audits.
        with database_connection() as connection:
            row = connection.execute(
                "SELECT bili_uid FROM join_request_audits WHERE group_id=? AND qq=? AND result='approved' "
                "AND action_status IN ('applied','not_required') AND bili_uid IS NOT NULL ORDER BY id DESC LIMIT 1",
                (str(group_id), str(qq))).fetchone()
            return int(row[0]) if row else None

    async def run_group(self, group_id):
        group_id = str(group_id)
        group = self.config.get_group_config(group_id)
        if not group or not group.enabled:
            raise ValueError('群未启用')
        self._policy(group)
        if group_id in self.running:
            raise ValueError('本群巡检正在运行')
        self.running.add(group_id)
        try:
            members = await self._members(group_id)
            report = {'id': secrets.token_urlsafe(18), 'group_id': group_id, 'created_at': self.clock(),
                      'policy_hash': self._fingerprint(group), 'member_count': len(members), 'items': []}
            for qq, member in members.items():
                if member.get('role') != 'member' or qq == str(self.runtime.client.self_id):
                    continue
                uid = await run_storage(self._group_uid, group_id, qq)
                binding = await run_storage(Records.get_bili_binding, uid) if uid else None
                uids = [uid] if binding and binding.owner_qq == qq else []
                for uid in uids:
                    # Only approved ownership bindings are considered. Historical
                    # groups_json is deliberately not used as membership proof.
                    observation = await self._observe(group, uid)
                    grace_key = f'{group_id}:{qq}:{uid}'
                    first = await run_storage(State.get, 'patrol_grace', grace_key)
                    if observation['state'] == 'failed':
                        if not first or first.get('policy_hash') != report['policy_hash']:
                            first = {'first_negative_at': self.clock(), 'policy_hash': report['policy_hash']}
                            await run_storage(State.set, 'patrol_grace', grace_key, first)
                    else:
                        await run_storage(State.delete, 'patrol_grace', grace_key)
                        first = None
                    report['items'].append({'qq': qq, 'uid': uid, **observation,
                                            'first_negative_at': first['first_negative_at'] if first else None})
            await run_storage(State.set, 'patrol_reports', report['id'], report)
            await run_storage(State.update, 'patrol_schedule', group_id,
                              lambda prior: {**(prior or {}), 'last_run': self.clock()})
            return report
        finally:
            self.running.discard(group_id)

    async def act(self, report_id, action, qq=None, *, automatic=False):
        if action not in {'remind', 'kick'}:
            raise ValueError('巡检动作只能为 remind 或 kick')
        report = await run_storage(State.get, 'patrol_reports', str(report_id))
        if not report:
            raise ValueError('巡检报告不存在')
        group = self.config.get_group_config(report['group_id'])
        if not group or not group.enabled or self._fingerprint(group) != report['policy_hash']:
            raise ValueError('群策略已改变，请重新巡检')
        policy = self._policy(group)
        if self.clock() - report['created_at'] > 86400:
            raise ValueError('报告超过24小时，请重新巡检')
        results = []
        dispatched = 0
        cap = min(50, max(1, int(policy.get('max_actions_per_run', 5))))
        for item in report['items']:
            if item['state'] != 'failed' or (qq is not None and str(item['qq']) != str(qq)):
                continue
            if dispatched >= cap:
                results.append({'qq': item['qq'], 'uid': item['uid'], 'state': 'skipped', 'detail': '达到本次动作数量上限'})
                continue
            user, uid = item['qq'], item['uid']
            locks = self.runtime.service.decisions.locks
            async with locks.hold(*identity_decision_keys(user, uid)):
                result = await self._act_member(report, group, policy, item, action, automatic)
                results.append({'qq': user, 'uid': uid, **result})
                if result['state'] in {'applied', 'pending'}:
                    dispatched += 1
        return {'report_id': str(report_id), 'action': action, 'results': results}

    async def _act_member(self, report, group, policy, item, action, automatic):
        user, uid = item['qq'], item['uid']
        if automatic and (not parse_bool(policy['enabled']) or policy['mode'] != action):
            return {'state': 'skipped', 'detail': '自动动作未启用'}
        if await self.runtime.service.decisions.has_pending_identity(user, uid):
            return {'state': 'skipped', 'detail': '存在未确认的审批，保留身份占用'}
        binding = await run_storage(Records.get_bili_binding, uid)
        if not binding or binding.owner_qq != user:
            return {'state': 'skipped', 'detail': '账号绑定已变更'}
        if await run_storage(self._group_uid, group.group_id, user) != uid:
            return {'state': 'skipped', 'detail': '本群已通过审核的账号已变更'}
        members = await self._members(group.group_id)
        member = members.get(user)
        client = self.runtime.client
        if not member or member.get('role') != 'member' or user == str(client.self_id):
            return {'state': 'skipped', 'detail': '用户已退群、角色未知或为群主/管理员/机器人'}
        if self._fingerprint(self.config.get_group_config(group.group_id)) != report['policy_hash']:
            return {'state': 'skipped', 'detail': '群策略已变更'}
        if action == 'kick':
            grace = max(60 if automatic else 0, int(policy.get('grace_seconds', 86400)))
            live = await run_storage(State.get, 'patrol_grace', f'{group.group_id}:{user}:{uid}', {})
            since = live.get('first_negative_at') if live.get('policy_hash') == report['policy_hash'] else None
            if since is None or self.clock() - since < grace:
                return {'state': 'waiting', 'detail': '尚在宽限期内，未移出群聊'}
        observation = await self._observe(group, uid)
        if observation['state'] != 'failed':
            await run_storage(State.delete, 'patrol_grace', f'{group.group_id}:{user}:{uid}')
            return {'state': 'skipped', 'detail': '复核未得到明确不合格结论，未执行动作'}
        current_group = self.config.get_group_config(group.group_id)
        if not current_group or not current_group.enabled or self._fingerprint(current_group) != report['policy_hash']:
            return {'state': 'skipped', 'detail': '复核期间群策略已变更，请重新巡检'}
        binding = await run_storage(Records.get_bili_binding, uid)
        if not binding or binding.owner_qq != user or await run_storage(self._group_uid, group.group_id, user) != uid:
            return {'state': 'skipped', 'detail': '复核期间绑定已变更'}
        if await self.runtime.service.decisions.has_pending_identity(user, uid):
            return {'state': 'skipped', 'detail': '复核期间出现未确认的审批'}
        latest_member = (await self._members(group.group_id)).get(user)
        if not latest_member or latest_member.get('role') != 'member' or user == str(self.runtime.client.self_id):
            return {'state': 'skipped', 'detail': '复核期间成员角色或成员状态已变更'}
        key = f'{group.group_id}:{user}:{uid}:{action}'
        now = self.clock()
        def reserve(previous):
            if previous and previous.get('state') == 'pending':
                raise ValueError('此成员存在结果未确认的巡检动作，请人工核对，禁止重复发送')
            if previous and previous.get('state') == 'applied':
                if action == 'kick' or now - previous.get('updated_at', 0) < max(3600, int(policy.get('remind_interval', 86400))):
                    raise ValueError('本成员的此动作已经执行，未重复发送')
            return {'state': 'pending', 'report_id': report['id'], 'group_id': group.group_id,
                    'qq': user, 'uid': uid, 'action': action, 'updated_at': now}
        try:
            intent = await run_storage(State.update, 'patrol_actions', key, reserve)
        except ValueError as exc:
            return {'state': 'skipped', 'detail': str(exc)}
        try:
            if action == 'kick':
                await client.set_group_kick(group_id=int(group.group_id), user_id=int(user), reject_add_request=False)
            else:
                message = (f'群{group.group_id}资格复查：' + '；'.join(observation['reasons'])
                           + '。请检查B站账号并恢复本群要求，或联系管理员复核。')
                if policy.get('remind_channel', 'group') == 'group':
                    await client.send_group_msg(group_id=int(group.group_id), message=[
                        {'type': 'at', 'data': {'qq': user}}, {'type': 'text', 'data': {'text': ' ' + message}}])
                else:
                    await client.send_private_msg(user_id=int(user), message=message)
        except DefiniteActionFailure:
            intent.update(state='failed', updated_at=self.clock())
            await run_storage(State.set, 'patrol_actions', key, intent)
            return {'state': 'failed', 'detail': 'QQ接口明确拒绝了动作'}
        except Exception:
            # Intent remains pending through disconnect, timeout or cancellation.
            return {'state': 'pending', 'detail': 'QQ动作结果未确认，请人工核对；不会自动重复发送'}
        intent.update(state='applied', updated_at=self.clock())
        await run_storage(State.set, 'patrol_actions', key, intent)
        return {'state': 'applied'}

    async def on_group_decrease(self, group_id, qq):
        group = self.config.get_group_config(str(group_id))
        if not group or not group.release_binding_on_leave:
            return {'state': 'retained'}
        uids = await run_storage(Records.get_owned_bili_uids, str(qq))
        async with self.runtime.service.decisions.locks.hold(*identity_decision_keys(str(qq), None),
                                                            *(f'identity:uid:{uid}' for uid in uids)):
            for uid in uids:
                if await self.runtime.service.decisions.has_pending_identity(str(qq), uid):
                    return {'state': 'retained', 'detail': '存在未确认审批'}
            client = self.runtime.client
            if client is None:
                return {'state': 'retained', 'detail': 'QQ连接不可用'}
            try:
                groups = await client.get_group_list()
                if (not isinstance(groups, list) or len(groups) > 500
                        or any(not isinstance(row, dict) or not str(row.get('group_id', '')).isdigit() for row in groups)):
                    return {'state': 'retained', 'detail': '群成员范围无法完整确认'}
                visible = {str(int(row['group_id'])) for row in groups}
                def approved_groups():
                    with database_connection() as connection:
                        return {str(row[0]) for row in connection.execute(
                            "SELECT DISTINCT group_id FROM join_request_audits WHERE qq=? AND result='approved' "
                            "AND action_status IN ('applied','not_required')", (str(qq),))}
                historical = await run_storage(approved_groups)
                if historical - visible - {str(group_id)}:
                    return {'state': 'retained', 'detail': '曾使用此绑定的其他群当前不可查询，保留归属待人工核实'}
                for joined in groups:
                    members = await self._members(str(joined['group_id']))
                    if str(qq) in members:
                        return {'state': 'retained', 'detail': '此QQ仍在机器人管理的其他群中'}
            except Exception:
                return {'state': 'retained', 'detail': '其他群的成员状态无法确认'}
            def release():
                with database_connection() as connection, connection:
                    connection.execute('BEGIN IMMEDIATE')
                    if connection.execute("SELECT 1 FROM join_request_audits WHERE action_status='pending' AND qq=? LIMIT 1", (str(qq),)).fetchone():
                        return 0
                    cursor = connection.execute('DELETE FROM bili_bindings WHERE owner_qq=?', (str(qq),))
                    record = {'group_id': str(group_id), 'qq': str(qq), 'uids': uids, 'released_at': self.clock()}
                    connection.execute('INSERT INTO runtime_state(namespace,key,value_json,updated_at) VALUES(?,?,?,?)',
                        ('binding_releases', secrets.token_urlsafe(18), json.dumps(record), self.clock()))
                    return cursor.rowcount
            count = await run_storage(release)
            return {'state': 'released' if count else 'retained', 'count': count}

    def reports(self, limit=100):
        with database_connection() as connection:
            rows = connection.execute('SELECT value_json FROM runtime_state WHERE namespace=? ORDER BY updated_at DESC LIMIT ?',
                                      ('patrol_reports', min(1000, max(1, int(limit))))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def status(self):
        return {'running': sorted(self.running), 'requests_per_second': self.limiter.rate,
                'actions': State.list_namespace('patrol_actions', 1000), 'default_mode': 'report'}

    async def _scheduled_run(self, group_id):
        try:
            report = await self.run_group(group_id)
            group = self.config.get_group_config(group_id)
            policy = self._policy(group) if group else {}
            await self.runtime.notify(f'群{group_id}资格巡检完成：明确不合格 '
                                      f"{sum(item['state'] == 'failed' for item in report['items'])} 项，请查看管理台。")
            if policy.get('mode') in {'remind', 'kick'}:
                await self.act(report['id'], policy['mode'], automatic=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Scheduled group patrol incomplete')

    async def loop(self):
        try:
            while True:
                for group in list(self.config.get_config().groups.values()):
                    try:
                        policy = self._policy(group)
                        if not group.enabled or not parse_bool(policy['enabled']) or group.group_id in self.running or len(self.jobs) >= 2:
                            continue
                        now = self.clock()
                        previous = await run_storage(State.get, 'patrol_schedule', group.group_id, {})
                        slot = int(now // 60)
                        if policy.get('cron'):
                            if previous.get('last_slot') == slot or not CronSchedule(policy['cron'], policy.get('timezone', 'Asia/Shanghai')).matches(now):
                                continue
                        elif now - previous.get('last_run', 0) < max(3600, int(policy.get('interval_seconds', 86400))):
                            continue
                        await run_storage(State.set, 'patrol_schedule', group.group_id, {'last_run': now, 'last_slot': slot})
                        task = asyncio.create_task(self._scheduled_run(group.group_id), name='patrol:' + group.group_id)
                        self.jobs.add(task)
                        task.add_done_callback(self.jobs.discard)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception('Group patrol scheduling incomplete')
                await asyncio.sleep(20)
        finally:
            await self.close()

    async def close(self):
        tasks = list(self.jobs)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
