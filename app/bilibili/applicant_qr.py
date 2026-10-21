"""Ephemeral applicant login. Never writes or reads the operator credential."""
import hashlib
import hmac
import secrets
import time

from .credentials import CredentialService, PASSPORT, cookie_text


class ApplicantQrService(CredentialService):
    async def start(self, challenge_id, nonce_hash, uid):
        async with self.lock:
            return await self._start(challenge_id, nonce_hash, uid)

    async def _start(self, challenge_id, nonce_hash, uid):
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value['expires'] > now and value['challenge_id'] != challenge_id}
        if len(self.sessions) >= 32:
            raise ValueError('扫码会话过多，请稍后重试')
        payload = (await self.request('GET', PASSPORT + 'qrcode/generate')).json()
        data = payload.get('data') or {}
        if payload.get('code') != 0 or not data.get('url') or not data.get('qrcode_key'):
            raise ValueError('B站扫码接口暂时不可用，请稍后重试')
        identifier = secrets.token_urlsafe(24)
        self.sessions[identifier] = {'challenge_id': challenge_id, 'nonce_hash': nonce_hash,
                                    'uid': int(uid), 'key': data['qrcode_key'], 'expires': now + 180, 'last_poll': 0}
        return {'session_id': identifier, 'url': str(data['url']), 'expires_at': now + 180}

    async def poll(self, identifier, challenge_id, secret):
        async with self.lock:
            session = self.sessions.get(identifier)
            if (not session or session['challenge_id'] != challenge_id or not isinstance(secret, str)
                    or not hmac.compare_digest(session['nonce_hash'], hashlib.sha256(secret.encode()).hexdigest())):
                raise ValueError('扫码会话与本次验证不匹配')
            if session['expires'] <= time.time():
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
            # Credentials exist only in this call frame and httpx response. Do
            # not call the operator _save()/set_credentials() even on success.
            candidate = cookie_text({cookie.name: cookie.value for cookie in response.cookies.jar})
            self.sessions.pop(identifier, None)
            try:
                identity = await self.identity(candidate)
                if str(identity) != str(session['uid']):
                    return {'state': 'wrong_identity', 'detail': '扫码账号与申请填写的UID不一致，请使用正确账号重新扫码'}
                return {'state': 'verified'}
            finally:
                self.client.cookies.clear()
