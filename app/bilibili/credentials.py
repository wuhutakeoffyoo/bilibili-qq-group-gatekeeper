"""Native operator credential login/refresh; never mixes applicant credentials."""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import ConfigManager
from .client import HEADERS, NAV_URL, reset_wbi_key_cache
from .pacing import HTTP_LIMITER, OBSERVATIONS

PASSPORT = 'https://passport.bilibili.com/x/passport-login/web/'
PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----'''


def cookie_fields(cookie):
    jar = SimpleCookie()
    try:
        jar.load(cookie)
    except CookieError:
        return {}
    return {key: value.value for key, value in jar.items()}


def cookie_text(values):
    return '; '.join(f'{key}={value}' for key, value in values.items() if value)


class CredentialService:
    def __init__(self, config=ConfigManager, *, client=None, notify=None):
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=20, trust_env=False)
        self.owns_client = client is None
        self.notify = notify
        self.lock = asyncio.Lock()
        self.sessions = {}
        self.last_status = {'state': 'unchecked'}

    async def request(self, method, url, *, cookie='', **kwargs):
        await HTTP_LIMITER.acquire()
        headers = {**HEADERS, **kwargs.pop('headers', {})}
        # Override httpx's persistent cookie jar even for anonymous QR calls.
        headers['Cookie'] = cookie
        try:
            response = await self.client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError:
            # QR keys and refresh correspondence paths are capabilities. Do
            # not propagate a request URL into server tracebacks or API errors.
            raise ValueError('B站凭证接口暂时无法连接，请稍后重试') from None
        return response

    async def identity(self, cookie):
        response = await self.request('GET', NAV_URL, cookie=cookie)
        data = response.json()
        details = data.get('data') or {}
        if data.get('code') != 0 or details.get('isLogin') is not True:
            raise ValueError('B站登录状态未能确认')
        return str(details['mid'])

    def _save(self, cookie, token, *, original_cookie=None, pending=None, expected_generation=None):
        snapshot = self.config.get_snapshot(include_secrets=True)
        current, version = snapshot['config'], snapshot['version']
        if original_cookie is not None and current['bili_cookie'] != original_cookie:
            raise ValueError('凭证已被其他操作更新，本次结果已丢弃')
        if expected_generation is not None and current.get('credential_generation', 0) != expected_generation:
            raise ValueError('凭证版本已变更，本次结果已丢弃，请重试')
        current['bili_cookie'] = cookie
        current['bili_refresh_token'] = token
        current['credential_generation'] = int(current.get('credential_generation', 0)) + 1
        current['bili_refresh_pending'] = pending or {}
        self.config.replace_config(current, expected_version=version, actor='credential-service')
        OBSERVATIONS.clear()
        reset_wbi_key_cache()

    async def ensure_fingerprints(self):
        async with self.lock:
            snapshot = self.config.get_snapshot(include_secrets=True)
            original = snapshot['config']['bili_cookie']
            generation = snapshot['config'].get('credential_generation', 0)
            values = cookie_fields(original)
            if values.get('buvid3') and values.get('buvid4'):
                return {'state': 'present'}
            response = await self.request('GET', 'https://api.bilibili.com/x/frontend/finger/spi', cookie=original)
            payload = response.json()
            if payload.get('code') != 0:
                return {'state': 'unavailable'}
            data = payload.get('data') or {}
            for name, key in [('buvid3', 'b_3'), ('buvid4', 'b_4')]:
                if not values.get(name) and data.get(key):
                    values[name] = str(data[key])
            values.setdefault('b_nut', str(int(time.time())))
            if not values.get('buvid3'):
                return {'state': 'unavailable'}
            self._save(cookie_text(values), str(snapshot['config'].get('bili_refresh_token', '')),
                       original_cookie=original, expected_generation=generation,
                       pending=snapshot['config'].get('bili_refresh_pending') or {})
            return {'state': 'updated'}

    async def start_qr_login(self):
        snapshot = self.config.get_snapshot(include_secrets=True)
        response = await self.request('GET', PASSPORT + 'qrcode/generate')
        payload = response.json()
        data = payload.get('data') or {}
        if payload.get('code') != 0 or not data.get('qrcode_key') or not data.get('url'):
            raise ValueError('B站扫码登录接口不可用')
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items() if value['expires'] > now}
        if len(self.sessions) >= 20:
            raise ValueError('登录会话过多，请稍后重试')
        identifier = secrets.token_urlsafe(24)
        self.sessions[identifier] = {'key': data['qrcode_key'], 'expires': now + 180,
                                     'cookie': snapshot['config']['bili_cookie'], 'last_poll': 0.0,
                                     'generation': snapshot['config'].get('credential_generation', 0)}
        return {'session_id': identifier, 'url': data['url'], 'expires_at': now + 180}

    async def poll_qr_login(self, identifier):
        async with self.lock:
            session = self.sessions.get(identifier)
            if not session or session['expires'] <= time.time():
                self.sessions.pop(identifier, None)
                return {'state': 'expired'}
            if time.time() - session['last_poll'] < 2:
                return {'state': 'waiting'}
            session['last_poll'] = time.time()
            response = await self.request('GET', PASSPORT + 'qrcode/poll', params={'qrcode_key': session['key']})
            payload = response.json()
            data = payload.get('data') or {}
            code = data.get('code') if payload.get('code') == 0 else None
            if code in {86101, 86090}:
                return {'state': 'waiting' if code == 86101 else 'confirming'}
            if code == 86038:
                self.sessions.pop(identifier, None)
                return {'state': 'expired'}
            if code != 0:
                return {'state': 'unavailable'}
            values = dict(response.cookies.items())
            candidate = cookie_text(values)
            await self.identity(candidate)
            self._save(candidate, str(data.get('refresh_token') or ''), original_cookie=session['cookie'],
                       expected_generation=session['generation'])
            self.sessions.pop(identifier, None)
            self.last_status = {'state': 'valid', 'checked_at': time.time()}
            return {'state': 'success'}

    async def _confirm(self, cookie, pending):
        before = self.config.get_snapshot(include_secrets=True)
        if before['config']['bili_cookie'] != cookie or before['config'].get('bili_refresh_pending') != pending:
            return False
        generation = before['config'].get('credential_generation', 0)
        response = await self.request('POST', PASSPORT + 'confirm/refresh', cookie=cookie,
            data={'csrf': cookie_fields(cookie).get('bili_jct', ''), 'refresh_token': pending['old_refresh_token']})
        if response.json().get('code') != 0:
            return False
        snapshot = self.config.get_snapshot(include_secrets=True)
        current, version = snapshot['config'], snapshot['version']
        if current['bili_cookie'] == cookie and current.get('credential_generation', 0) == generation:
            current['bili_refresh_pending'] = {}
            self.config.replace_config(current, expected_version=version, actor='credential-confirm')
        return True

    async def refresh_if_due(self):
        async with self.lock:
            current = self.config.get_snapshot(include_secrets=True)['config']
            original = current['bili_cookie']
            generation = current.get('credential_generation', 0)
            if not original:
                return {'state': 'not_configured'}
            pending = current.get('bili_refresh_pending', {}) or {}
            if pending:
                return {'state': 'confirmed' if await self._confirm(original, pending) else 'confirmation_pending'}
            values = cookie_fields(original)
            token = str(current.get('bili_refresh_token', '') or values.get('ac_time_value', ''))
            if not token or not values.get('bili_jct'):
                return {'state': 'refresh_token_missing'}
            owner = await self.identity(original)
            info = (await self.request('GET', PASSPORT + 'cookie/info', cookie=original, params={'csrf': values['bili_jct']})).json()
            if info.get('code') != 0:
                return {'state': 'unavailable'}
            data = info.get('data') or {}
            if not data.get('refresh'):
                return {'state': 'not_due'}
            timestamp = int(data.get('timestamp') or time.time() * 1000)
            key = serialization.load_pem_public_key(PUBLIC_KEY)
            encrypted = key.encrypt(f'refresh_{timestamp}'.encode(), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).hex()
            page = await self.request('GET', 'https://www.bilibili.com/correspond/1/' + encrypted, cookie=original)
            match = re.search(r'<div[^>]*id=[\"\']1-name[\"\'][^>]*>([^<]+)</div>', page.text)
            if not match:
                return {'state': 'refresh_csrf_unavailable'}
            response = await self.request('POST', PASSPORT + 'cookie/refresh', cookie=original,
                data={'csrf': values['bili_jct'], 'refresh_csrf': match.group(1), 'refresh_token': token, 'source': 'main_web'})
            result = response.json()
            if result.get('code') != 0:
                return {'state': 'unavailable'}
            values.update(dict(response.cookies.items()))
            candidate = cookie_text(values)
            next_token = str((result.get('data') or {}).get('refresh_token') or '')
            if not next_token or await self.identity(candidate) != owner:
                raise ValueError('刷新后的凭证身份未通过验证，未替换现有配置')
            pending = {'old_refresh_token': token, 'created_at': time.time()}
            self._save(candidate, next_token, original_cookie=original, pending=pending, expected_generation=generation)
            confirmed = await self._confirm(candidate, pending)
            return {'state': 'refreshed' if confirmed else 'confirmation_pending'}

    async def status(self):
        cookie = self.config.get_bili_cookie()
        if not cookie:
            return {'state': 'not_configured'}
        try:
            await self.identity(cookie)
            self.last_status = {'state': 'valid', 'checked_at': time.time(),
                                'credential_fingerprint': hashlib.sha256(cookie.encode()).hexdigest()[:12]}
        except Exception:
            self.last_status = {'state': 'unconfirmed', 'checked_at': time.time()}
        return {**self.last_status, 'refresh_confirmation_pending': bool(
            getattr(self.config.get_config(), 'bili_refresh_pending', {}))}

    async def loop(self):
        while True:
            try:
                result = await self.refresh_if_due()
                self.last_status['refresh_state'] = result['state']
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_status = {'state': 'refresh_error', 'error_type': type(exc).__name__}
                if self.notify:
                    await self.notify('B站凭证自动续期未完成，请在管理台检查；当前凭证未因检查异常被清空。')
            await asyncio.sleep(3600)

    async def close(self):
        self.sessions.clear()
        if self.owns_client:
            await self.client.aclose()
