"""Read-only proof adapters. A positive needs an exact author, nonce and time.

Protocol references are pinned in docs/v3-implementation.md; protocol fixtures
do not establish live API availability for a particular Bilibili account.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .client import BiliApi, NAV_URL, _wbi_sign_query


@dataclass(frozen=True)
class ProofObservation:
    state: str
    detail: str = ""
    proof_id: str = ""


class ProofClient(BiliApi):
    async def observe(self, policy, uid: int, code: str, created_at: float, expires_at: float):
        try:
            if policy['mode'] == 'dm':
                return await self._dm(policy, uid, code, created_at, expires_at)
            if policy['mode'] == 'dynamic':
                return await self._dynamic(policy, uid, code, created_at, expires_at)
        except Exception:
            return ProofObservation('unavailable', 'B站验证接口暂时无法读取，请稍后重试或联系管理员')
        return ProofObservation('unavailable', '管理员尚未配置可用的验证通道')

    @staticmethod
    def _matches(sender, timestamp, text, uid, code, created_at, expires_at):
        try:
            return (int(sender) == int(uid) and created_at <= float(timestamp) <= min(time.time() + 5, expires_at)
                    and isinstance(text, str) and text.strip() == code)
        except (ValueError, TypeError):
            return False

    async def _dm(self, policy, uid, code, created_at, expires_at):
        nav = await self._get(NAV_URL)
        owner = (nav.get('data') or {})
        if nav.get('code') != 0 or owner.get('isLogin') is not True or str(owner.get('mid')) != str(policy.get('receiver_uid')):
            return ProofObservation('unavailable', '验证收信账号与管理员配置不一致，请联系管理员')
        keys = await self._get_wbi_keys()
        if not keys:
            return ProofObservation('unavailable', 'B站私信签名参数暂时无法读取，请稍后重试')
        query = _wbi_sign_query({'talker_id': uid, 'session_type': 1, 'size': 30, 'begin_seqno': 0},
                                *keys, wts=int(time.time()))
        payload = await self._get('https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs?' + query)
        data = payload.get('data') or {}
        if payload.get('code') != 0 or not isinstance(data.get('messages'), list):
            return ProofObservation('unavailable', 'B站私信暂时无法读取，请稍后重试')
        for message in data['messages']:
            if not isinstance(message, dict) or message.get('msg_type') != 1:
                continue
            try:
                content = json.loads(message.get('content', '{}'))
            except (ValueError, TypeError):
                continue
            if (str(message.get('receiver_id')) == str(policy['receiver_uid'])
                    and self._matches(message.get('sender_uid'), message.get('timestamp'),
                                      content.get('content') if isinstance(content, dict) else None,
                                      uid, code, created_at, expires_at)):
                return ProofObservation('verified', '已读取到指定账号发送的验证私信', str(message.get('msg_key', '')))
        return ProofObservation('waiting', '还未读取到验证私信，请用指定B站账号发送验证码后稍候重试')

    async def _dynamic(self, policy, uid, code, created_at, expires_at):
        # An image dynamic's comment oid can differ from its dynamic id. The
        # operator configures the actual comment oid/type; never guess a mapping.
        for page in range(1, min(10, max(1, int(policy.get('max_pages', 5)))) + 1):
            payload = await self._get('https://api.bilibili.com/x/v2/reply', {
                'oid': int(policy['reply_oid']), 'type': int(policy.get('reply_type', 17)), 'pn': page, 'sort': 0})
            data = payload.get('data') or {}
            replies = data.get('replies')
            if payload.get('code') != 0 or (replies is not None and not isinstance(replies, list)):
                return ProofObservation('unavailable', 'B站动态评论暂时无法读取，请稍后重试')
            for reply in replies or []:
                if not isinstance(reply, dict):
                    continue
                member, content = reply.get('member') or {}, reply.get('content') or {}
                if self._matches(member.get('mid'), reply.get('ctime'), content.get('message'),
                                 uid, code, created_at, expires_at):
                    return ProofObservation('verified', '已读取到指定账号发送的动态评论', str(reply.get('rpid', '')))
            if not replies:
                break
        return ProofObservation('waiting', '还未读取到验证评论，请用指定B站账号在指定动态下单独发送验证码后重试')
