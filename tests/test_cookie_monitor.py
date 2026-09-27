import os
import smtplib
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import nonebot

nonebot.init()
from src.plugins.group_request_manager import cookie_monitor, email_notifier


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", cookie_monitor.BILI_NAV_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None, **_kwargs):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


class CookieValidityTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, payload):
        client = _FakeClient(response=_FakeResponse(payload))
        with patch.object(cookie_monitor.httpx, "AsyncClient", return_value=client):
            return await cookie_monitor.check_cookie_validity("cookie=value")

    async def test_valid_cookie(self):
        detail = await self._check({"code": 0, "data": {"isLogin": True}})
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.VALID)

    async def test_not_logged_in_cookie_is_expired(self):
        detail = await self._check({"code": 0, "data": {"isLogin": False}})
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.EXPIRED)

    async def test_not_login_code_is_expired_without_data(self):
        detail = await self._check({"code": -101, "message": "账号未登录", "data": None})
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.EXPIRED)

    async def test_other_business_code_is_not_reported_as_expired(self):
        detail = await self._check({"code": -412, "message": "请求被拦截", "data": {}})
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.API_ERROR)

    async def test_missing_data_is_api_error(self):
        detail = await self._check({"code": 0, "data": None})
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.API_ERROR)

    async def test_request_error_is_network_error(self):
        error = httpx.ConnectError(
            "offline", request=httpx.Request("GET", cookie_monitor.BILI_NAV_URL)
        )
        client = _FakeClient(error=error)
        with patch.object(cookie_monitor.httpx, "AsyncClient", return_value=client):
            detail = await cookie_monitor.check_cookie_validity("cookie=value")
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.NETWORK_ERROR)

    async def test_empty_cookie_is_expired_without_request(self):
        detail = await cookie_monitor.check_cookie_validity("")
        self.assertEqual(detail.result, cookie_monitor.CookieCheckResult.EXPIRED)


class CookieNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cookie_monitor.reset_dedup_state()

    async def test_successful_notification_is_deduplicated(self):
        expired = cookie_monitor.CookieCheckDetail(cookie_monitor.CookieCheckResult.EXPIRED)
        sender = AsyncMock(return_value=True)
        with patch.object(cookie_monitor.CookieManager, "get_cookie", return_value="value"), patch.object(
            cookie_monitor, "check_cookie_validity", AsyncMock(return_value=expired)
        ), patch.object(cookie_monitor.email_notifier, "send_notification", sender):
            await cookie_monitor.run_cookie_check_once()
            await cookie_monitor.run_cookie_check_once()
        self.assertEqual(sender.await_count, 1)

    async def test_failed_notification_retries(self):
        expired = cookie_monitor.CookieCheckDetail(cookie_monitor.CookieCheckResult.EXPIRED)
        sender = AsyncMock(return_value=False)
        with patch.object(cookie_monitor.CookieManager, "get_cookie", return_value="value"), patch.object(
            cookie_monitor, "check_cookie_validity", AsyncMock(return_value=expired)
        ), patch.object(cookie_monitor.email_notifier, "send_notification", sender):
            await cookie_monitor.run_cookie_check_once()
            await cookie_monitor.run_cookie_check_once()
        self.assertEqual(sender.await_count, 2)

    async def test_recovery_allows_a_later_notification(self):
        expired = cookie_monitor.CookieCheckDetail(cookie_monitor.CookieCheckResult.EXPIRED)
        valid = cookie_monitor.CookieCheckDetail(cookie_monitor.CookieCheckResult.VALID)
        checker = AsyncMock(side_effect=[expired, valid, expired])
        sender = AsyncMock(return_value=True)
        with patch.object(cookie_monitor.CookieManager, "get_cookie", return_value="value"), patch.object(
            cookie_monitor, "check_cookie_validity", checker
        ), patch.object(cookie_monitor.email_notifier, "send_notification", sender):
            await cookie_monitor.run_cookie_check_once()
            await cookie_monitor.run_cookie_check_once()
            await cookie_monitor.run_cookie_check_once()
        self.assertEqual(sender.await_count, 2)

    async def test_network_error_sends_abnormal_notification_once(self):
        network_error = cookie_monitor.CookieCheckDetail(
            cookie_monitor.CookieCheckResult.NETWORK_ERROR,
            "offline",
        )
        sender = AsyncMock(return_value=True)
        with patch.object(cookie_monitor.CookieManager, "get_cookie", return_value="value"), patch.object(
            cookie_monitor, "check_cookie_validity", AsyncMock(return_value=network_error)
        ), patch.object(cookie_monitor.email_notifier, "send_notification", sender):
            await cookie_monitor.run_cookie_check_once()
            await cookie_monitor.run_cookie_check_once()
        self.assertEqual(sender.await_count, 1)
        subject = sender.await_args.args[0]
        self.assertIn("Cookie 检查异常", subject)

    async def test_api_error_sends_abnormal_notification(self):
        api_error = cookie_monitor.CookieCheckDetail(
            cookie_monitor.CookieCheckResult.API_ERROR,
            "blocked",
            api_code=-412,
        )
        sender = AsyncMock(return_value=True)
        with patch.object(cookie_monitor.CookieManager, "get_cookie", return_value="value"), patch.object(
            cookie_monitor, "check_cookie_validity", AsyncMock(return_value=api_error)
        ), patch.object(cookie_monitor.email_notifier, "send_notification", sender):
            await cookie_monitor.run_cookie_check_once()
        sender.assert_awaited_once()
        self.assertIn("B站接口异常", sender.await_args.args[0])

    async def test_abnormal_notification_failure_retries(self):
        network_error = cookie_monitor.CookieCheckDetail(
            cookie_monitor.CookieCheckResult.NETWORK_ERROR,
            "offline",
        )
        sender = AsyncMock(return_value=False)
        with patch.object(cookie_monitor.CookieManager, "get_cookie", return_value="value"), patch.object(
            cookie_monitor, "check_cookie_validity", AsyncMock(return_value=network_error)
        ), patch.object(cookie_monitor.email_notifier, "send_notification", sender):
            await cookie_monitor.run_cookie_check_once()
            await cookie_monitor.run_cookie_check_once()
        self.assertEqual(sender.await_count, 2)

    def test_interval_is_clamped(self):
        with patch.dict(os.environ, {cookie_monitor.ENV_CHECK_INTERVAL: "1"}):
            self.assertEqual(
                cookie_monitor.get_check_interval_seconds(),
                cookie_monitor.MIN_CHECK_INTERVAL_SECONDS,
            )
        with patch.dict(os.environ, {cookie_monitor.ENV_CHECK_INTERVAL: "999999"}):
            self.assertEqual(
                cookie_monitor.get_check_interval_seconds(),
                cookie_monitor.MAX_CHECK_INTERVAL_SECONDS,
            )


class EmailNotifierTests(unittest.IsolatedAsyncioTestCase):
    def _environment(self):
        return {
            email_notifier.ENV_SMTP_HOST: "smtp.example.com",
            email_notifier.ENV_SMTP_PORT: "465",
            email_notifier.ENV_SMTP_USER: "sender@example.com",
            email_notifier.ENV_SMTP_PASSWORD: "secret",
            email_notifier.ENV_FROM_ADDR: "sender@example.com",
            email_notifier.ENV_TO_ADDR: "recipient@example.com",
        }

    async def test_missing_configuration_does_not_send(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            email_notifier, "_send_smtp"
        ) as sender:
            result = await email_notifier.send_notification("subject", "body")
        self.assertFalse(result)
        sender.assert_not_called()

    async def test_successful_send(self):
        with patch.dict(os.environ, self._environment(), clear=True), patch.object(
            email_notifier, "_send_smtp"
        ) as sender:
            result = await email_notifier.send_notification("subject", "body")
        self.assertTrue(result)
        sender.assert_called_once()

    async def test_authentication_failure_is_contained(self):
        error = smtplib.SMTPAuthenticationError(535, b"denied")
        with patch.dict(os.environ, self._environment(), clear=True), patch.object(
            email_notifier, "_send_smtp", side_effect=error
        ):
            result = await email_notifier.send_notification("subject", "body")
        self.assertFalse(result)

    def test_invalid_port_falls_back_to_ssl_default(self):
        env = self._environment()
        env[email_notifier.ENV_SMTP_PORT] = "70000"
        with patch.dict(os.environ, env, clear=True):
            config = email_notifier._get_smtp_config()
        self.assertEqual(config["port"], email_notifier.DEFAULT_SMTP_PORT)


if __name__ == "__main__":
    unittest.main()
