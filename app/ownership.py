"""Optional one-use Bilibili ownership challenges tied to real QQ requests.

Only the join-request service issues capabilities. A public page cannot choose
an arbitrary QQ/UID and grant it ownership. The challenge state keeps a nonce
hash. Private decision/audit payloads may retain the exact feedback link for
durability and request-specific replay, and require protected database access.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

from app.bilibili.proof import ProofClient
from app.config import ConfigManager
from app.database.async_storage import run_storage
from app.database.legacy import database_connection
from app.database.repository import RuntimeStateRepository as State
from src.gatekeeper.config import parse_bool
from src.gatekeeper.decisions import DecisionRequest

NAMESPACE = 'ownership'


class OwnershipPrompt(str):
    def __new__(cls, message, *, hold=False, notify=True):
        value = super().__new__(cls, message)
        value.hold, value.notify = hold, notify
        return value


def policy_hash(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class OwnershipService:
    def __init__(self, config=ConfigManager, *, api_factory=None, clock=time.time, qr_service=None, resume=None):
        self.config, self.api_factory, self.clock = config, api_factory or ProofClient, clock
        self.qr, self.resume = qr_service, resume

    @staticmethod
    def _policy(group):
        policy = dict(group.verification or {})
        mode = str(policy.get('mode', 'off')).lower()
        if mode in {'', 'off', 'none', 'disabled'} or not parse_bool(policy.get('enabled', True)):
            return None
        if mode not in {'dm', 'dynamic', 'qr'}:
            raise ValueError('归属验证 mode 只能为 off、dm、dynamic 或 qr')
        policy['mode'] = mode
        if mode == 'dm':
            value = str(policy.get('receiver_uid', ''))
            if not value.isdigit() or len(value) > 20 or int(value) <= 0:
                raise ValueError('归属验证未配置有效的私信收信 UID，请联系管理员')
            policy['receiver_uid'] = str(int(value))
        if mode == 'dynamic':
            value = str(policy.get('reply_oid', ''))
            if not value.isdigit() or len(value) > 20 or int(value) <= 0:
                raise ValueError('归属验证未配置有效的动态评论区 ID，请联系管理员')
            policy['reply_oid'] = str(int(value))
            policy['reply_type'] = int(policy.get('reply_type', 17))
            policy['max_pages'] = min(10, max(1, int(policy.get('max_pages', 5))))
        if mode == 'dynamic' and not str(policy.get('dynamic_url', '')).startswith('https://'):
            raise ValueError('归属验证未配置 HTTPS 动态地址，请联系管理员')
        policy['ttl_seconds'] = min(3600, max(120, int(policy.get('ttl_seconds', 900))))
        return policy

    @staticmethod
    def public_base(policy, config):
        base = str(policy.get('public_url') or getattr(config, 'ownership_public_url', '')).rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('管理员尚未配置有效的HTTPS归属验证网址，请联系管理员完成验证')
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError('归属验证网址端口无效')
        return base

    @staticmethod
    def _key(group_id, qq, uid):
        return hashlib.sha256(json.dumps([str(group_id), str(qq), int(uid)]).encode()).hexdigest()[:32]

    async def before_decision(self, context):
        try:
            policy = self._policy(context.group_config)
        except ValueError as exc:
            return str(exc)
        if policy is None:
            return None
        if not context.bili_uid:
            return '归属验证需要有效的B站账号，请检查UID或昵称后重新申请。请不要填写B站昵称或UID以外的内容'
        key = self._key(context.group_id, context.user_id, context.bili_uid)
        now, fingerprint = self.clock(), policy_hash(policy)
        request_flag = str(context.event.flag)
        request = DecisionRequest(str(context.bot.self_id), str(context.group_id), str(context.event.sub_type), request_flag)
        def already_recorded():
            with database_connection() as connection:
                return connection.execute('SELECT 1 FROM join_request_audits WHERE request_key=? LIMIT 1',
                                          (request.request_key,)).fetchone() is not None
        if await run_storage(already_recorded):
            return '此申请已处理或等待确认，请使用原验证链接；完成验证后提交新的入群申请'
        def consume(previous):
            if (previous and previous.get('state') in {'verified', 'consumed'}
                    and previous.get('expires_at', 0) > now and previous.get('policy_hash') == fingerprint
                    and (policy['mode'] == 'qr' or previous.get('request_flag_hash') != hashlib.sha256(request_flag.encode()).hexdigest())
                    and (previous.get('state') == 'verified' or previous.get('consumed_for') == request.request_key)):
                return {**previous, 'state': 'consumed', 'consumed_for': request.request_key, 'consumed_at': now}
            return previous
        previous = await run_storage(State.update, NAMESPACE, key, consume)
        if (previous and previous.get('state') == 'consumed' and previous.get('consumed_for') == request.request_key
                and previous['expires_at'] > now and previous['policy_hash'] == fingerprint):
            return None
        if (previous and previous.get('request_flag_hash') == hashlib.sha256(request_flag.encode()).hexdigest()
                and previous['expires_at'] > now and previous['policy_hash'] == fingerprint):
            return OwnershipPrompt('此申请已提供归属验证链接，请使用原链接完成验证',
                                   hold=policy['mode'] == 'qr', notify=False)
        try:
            base = self.public_base(policy, self.config.get_config())
        except ValueError as exc:
            return str(exc)
        secret = secrets.token_urlsafe(18)
        ttl = min(3600, max(120, int(policy.get('ttl_seconds', 900))))
        state = {'id': key, 'group_id': str(context.group_id), 'qq': str(context.user_id),
                 'uid': int(context.bili_uid), 'state': 'pending', 'mode': policy['mode'],
                 'nonce_hash': hashlib.sha256(secret.encode()).hexdigest(), 'policy_hash': fingerprint,
                 'created_at': int(now), 'expires_at': now + ttl, 'last_attempt': 0,
                 'request_flag_hash': hashlib.sha256(request_flag.encode()).hexdigest()}
        if policy['mode'] == 'qr':
            state['request'] = {'bot_id': str(context.bot.self_id), 'group_id': str(context.group_id),
                                'qq': str(context.user_id), 'sub_type': str(context.event.sub_type),
                                'flag': request_flag, 'comment': 'UID:' + str(context.bili_uid)}
        await run_storage(State.set, NAMESPACE, key, state)
        link = base + '/verify#' + urlencode({'id': key, 'secret': secret})
        return OwnershipPrompt(('请完成B站账号扫码验证，机器人将重新检查本群条件：' if policy['mode'] == 'qr'
                                else '请先完成B站账号归属验证后重新申请：') + '\n' + link + '\n申请答案只填写B站UID或昵称',
                               hold=policy['mode'] == 'qr')

    def _authorized(self, state, secret):
        return bool(state and isinstance(secret, str) and 16 <= len(secret) <= 128
                    and hmac.compare_digest(str(state.get('nonce_hash', '')), hashlib.sha256(secret.encode()).hexdigest()))

    async def challenge(self, identifier, secret, *, check=False):
        if not isinstance(identifier, str) or len(identifier) != 32:
            raise ValueError('验证链接无效，请重新提交入群申请获取链接')
        now = self.clock()
        state = await run_storage(State.get, NAMESPACE, identifier)
        if not self._authorized(state, secret):
            raise ValueError('验证链接无效，请重新提交入群申请获取链接')
        group = self.config.get_group_config(state['group_id'])
        policy = self._policy(group) if group and group.enabled else None
        if not policy or policy_hash(policy) != state['policy_hash']:
            return {'state': 'expired', 'detail': '群验证配置已变更，请重新申请'}
        if state['state'] == 'consumed':
            return {'state': 'expired', 'detail': '此验证已用于一次入群申请，请查看QQ申请结果；结果未明确时请联系管理员核实'}
        if state['state'] == 'revoked':
            return {'state': 'expired', 'detail': '管理员已撤销此次验证，请联系管理员或重新提交入群申请'}
        if state['expires_at'] <= now:
            return {'state': 'expired', 'detail': '验证链接已过期，请重新提交入群申请获取新链接'}
        result = {'state': state['state'], 'group_id': state['group_id'], 'qq': state['qq'],
                  'uid': state['uid'], 'mode': policy['mode'], 'expires_at': state['expires_at'],
                  'code': 'GK-' + secret, 'receiver_uid': policy.get('receiver_uid'),
                  'dynamic_url': policy.get('dynamic_url'), 'detail': '完成验证后，请在有效期内用同一QQ和B站UID重新申请；答案只填写B站昵称或UID'}
        if not check or state['state'] == 'verified':
            return result
        if policy['mode'] == 'qr':
            return {**result, 'detail': '请点击生成二维码，使用所填UID对应的B站账号扫码'}
        # Persist the poll reservation, so multiple workers/processes cannot
        # multiply Bilibili traffic or race a rotated challenge.
        def reserve(current):
            if not self._authorized(current, secret) or current['state'] != 'pending':
                raise ValueError('验证链接已失效，请重新申请')
            if now - current.get('last_attempt', 0) < 5:
                raise ValueError('请等待5秒后再检查验证结果')
            return {**current, 'last_attempt': now}
        state = await run_storage(State.update, NAMESPACE, identifier, reserve)
        cookie = self.config.get_bili_cookie()
        if not cookie:
            return {**result, 'state': 'unavailable', 'detail': '管理员验证凭证未配置，请联系管理员'}
        api = self.api_factory(cookie, cache_scope='ownership:' + identifier)
        try:
            observation = await api.observe(policy, state['uid'], 'GK-' + secret, state['created_at'], state['expires_at'])
        finally:
            await api.close()
        if observation.state == 'verified':
            def verify(current):
                if (not self._authorized(current, secret) or current['state'] != 'pending'
                        or current['expires_at'] <= self.clock()):
                    raise ValueError('验证已失效，请重新提交申请')
                return {**current, 'state': 'verified', 'verified_at': self.clock(),
                        'proof_id_hash': hashlib.sha256(observation.proof_id.encode()).hexdigest()}
            await run_storage(State.update, NAMESPACE, identifier, verify)
        return {**result, 'state': observation.state, 'detail': observation.detail + '。完成后用同一QQ和UID重新申请，答案只填写昵称或UID'}

    async def start_qr(self, identifier, secret):
        details = await self.challenge(identifier, secret)
        if details.get('state') != 'pending' or details.get('mode') != 'qr':
            raise ValueError('本验证不处于可扫码状态')
        if self.qr is None:
            from app.bilibili.applicant_qr import ApplicantQrService
            self.qr = ApplicantQrService()
        def reserve(current):
            if not self._authorized(current, secret) or current['state'] != 'pending' or current['expires_at'] <= self.clock():
                raise ValueError('验证链接已失效，请重新申请')
            if self.clock() - current.get('last_qr_started', 0) < 5:
                raise ValueError('请等待5秒后再生成二维码')
            return {**current, 'last_qr_started': self.clock()}
        state = await run_storage(State.update, NAMESPACE, identifier, reserve)
        return await self.qr.start(identifier, state['nonce_hash'], state['uid'])

    async def poll_qr(self, identifier, secret, session_id):
        details = await self.challenge(identifier, secret)
        if details.get('mode') != 'qr' or details.get('state') != 'pending' or self.qr is None:
            return {'state': details.get('state', 'expired'), 'detail': details.get('detail', '请重新生成二维码')}
        result = await self.qr.poll(str(session_id), identifier, secret)
        if result['state'] != 'verified':
            return result
        before = await run_storage(State.get, NAMESPACE, identifier)
        group = self.config.get_group_config(before['group_id']) if before else None
        policy = self._policy(group) if group and group.enabled else None
        fingerprint = policy_hash(policy) if policy else ''
        def verify(current):
            if (not self._authorized(current, secret) or current['state'] != 'pending'
                    or current['expires_at'] <= self.clock() or fingerprint != current['policy_hash']):
                raise ValueError('验证条件已变化，请重新提交入群申请')
            return {**current, 'state': 'verified', 'verified_at': self.clock(), 'proof_type': 'native-qr'}
        state = await run_storage(State.update, NAMESPACE, identifier, verify)
        if self.resume:
            outcome = await self.resume(state)
            return {'state': 'verified', 'resume': outcome, 'detail': outcome.get('detail', '验证成功，请查看QQ申请结果')}
        return {'state': 'verified', 'detail': '验证成功，请用同一QQ和UID重新申请；答案只填写昵称或UID'}

    def list_challenges(self, limit=100):
        with database_connection() as connection:
            rows = [json.loads(row[0]) for row in connection.execute(
                'SELECT value_json FROM runtime_state WHERE namespace=? ORDER BY updated_at DESC LIMIT ?',
                (NAMESPACE, min(1000, max(1, int(limit)))))]
        return [{key: value for key, value in row.items()
                 if key not in {'nonce_hash', 'request_flag_hash', 'consumed_for', 'request'}} for row in rows if row]

    def status(self):
        rows = self.list_challenges(1000)
        return {'count': len(rows), 'pending': sum(row['state'] == 'pending' and row['expires_at'] > self.clock() for row in rows),
                'modes': ['dm', 'dynamic', 'qr'], 'issuance': 'QQ join request only'}

    def revoke(self, identifier, actor='admin'):
        def revoke(current):
            if not current:
                raise ValueError('验证记录不存在')
            return {**current, 'state': 'revoked', 'revoked_by': str(actor), 'revoked_at': self.clock()}
        State.update(NAMESPACE, identifier, revoke)
        return True

    async def close(self):
        if self.qr is not None:
            await self.qr.close()
