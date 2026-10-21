"""Authenticated ASGI management using actual revision and business SQLite rows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
import yaml

from app.config import ConfigManager, GroupConfig, ReviewStageConfig
from app.database import legacy
from app.database.async_storage import run_storage
from app.database.leaves import LeaveRecordManager
from app.database.records import JoinRequestRecordManager as Records
from app.gatekeeper.decisions import ScopedDecisionService
from app.bilibili.runtime import BiliRequestCoordinator
from app.web.admin_api import install_admin_routes

TOKEN = "test-admin-credential-" + "x" * 40


class Storage:
    async def add_audit(self, **kwargs):
        return await run_storage(Records.add_audit, **kwargs)

    async def begin_pending_decision(self, **kwargs):
        return await run_storage(Records.begin_pending_decision, **kwargs)

    async def finalize_pending_decision(self, audit_id, **kwargs):
        return await run_storage(Records.finalize_pending_decision, audit_id, **kwargs)

    async def get_pending_decision_count(self):
        return await run_storage(Records.get_pending_decision_count)

    async def get_pending_decisions(self, limit=20):
        return await run_storage(Records.get_pending_decisions, limit)


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.settings = SimpleNamespace(app_root=self.root, host="127.0.0.1")
        ConfigManager.reset_state()
        ConfigManager.initialize(self.settings)
        config = ConfigManager.get_config().model_copy(deep=True)
        config.webui_enabled, config.webui_token = True, TOKEN
        config.bili_cookie, config.bili_refresh_token = "SESSDATA=private-cookie", "private-refresh"
        config.smtp_password = "private-smtp"
        config.groups = {"90001": GroupConfig(group_id="90001", review_stages=[ReviewStageConfig(
            index=1, name="基础组", when="bili_account and no_conflict",
            on_true=["approve"], on_false=["reject"], on_unknown=["ignore"],
        )])}
        self.version = ConfigManager.replace_config(config, ConfigManager.get_version(), actor="test-setup")
        self.credentials = SimpleNamespace(
            last_status={"state": "unchecked"}, status=AsyncMock(return_value={"state": "valid"}),
            start_qr_login=AsyncMock(return_value={
                "session_id": "qr-test-id", "url": "https://passport.bilibili.com/qrcode/h5/login?key=opaque-login",
                "expires_at": time.time() + 180,
            }), poll_qr_login=AsyncMock(return_value={"state": "success"}),
            refresh_if_due=AsyncMock(return_value={"state": "not_due"}),
            ensure_fingerprints=AsyncMock(return_value={"state": "present"}),
        )
        self.ownership = SimpleNamespace(
            status=lambda: {"count": 1},
            list_challenges=lambda limit=100: [{"id": "task-one", "group_id": "90001", "qq": "10001", "uid": 88, "state": "pending"}],
            revoke=lambda identifier, actor="admin": identifier == "task-one",
        )
        self.patrol = SimpleNamespace(
            status=lambda: {"default_mode": "report"},
            reports=lambda limit=100: [{"id": "report-one", "group_id": "90001", "items": []}],
            run_group=AsyncMock(return_value={"id": "report-new", "items": []}),
            act=AsyncMock(return_value={"state": "applied"}),
        )
        self.decisions = ScopedDecisionService(Storage())
        self.service = SimpleNamespace(decisions=self.decisions, simulate=AsyncMock(return_value="模拟：无法确认关注状态"))
        self.runtime = SimpleNamespace(
            settings=self.settings, config=ConfigManager, service=self.service, credentials=self.credentials,
            ownership=self.ownership, patrol=self.patrol, started=True,
            clients={"test": SimpleNamespace(self_id="99000", connected=True)},
        )
        self.app = FastAPI()

        @self.app.get("/healthz")
        async def public_health():
            return {"status": "ok"}

        install_admin_routes(self.app, self.runtime)
        self.client = self.new_client()
        self.extra_clients = []

    async def asyncTearDown(self):
        await self.client.aclose()
        for client in self.extra_clients:
            await client.aclose()
        ConfigManager.reset_state()
        legacy.reset_database_state()
        self.environment.stop()
        self.directory.cleanup()

    def new_client(self, *, peer="127.0.0.1", base="http://127.0.0.1"):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=(peer, 1234)), base_url=base)

    async def login(self, client=None):
        client = client or self.client
        response = await client.post("/api/login", json={"token": TOKEN})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf"]
        return response

    def update(self, **changes):
        config = ConfigManager.get_config().model_copy(deep=True)
        for name, value in changes.items():
            setattr(config, name, value)
        return ConfigManager.replace_config(config, ConfigManager.get_version(), actor="local-test")

    async def test_console_is_nonce_protected_and_never_contains_login_credentials(self):
        response = await self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn("private-cookie", response.text)
        self.assertNotIn("__NONCE__", response.text)
        self.assertIn("script-src 'self' 'nonce-", response.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("localStorage", response.text)
        self.assertNotIn("innerHTML", response.text)
        for control in ("groupForm", "stageForms", "pendingTable", "qrStart", "patrolAct", "versionTable", "simulateForm"):
            self.assertIn('id="' + control + '"', response.text)

    async def test_every_sensitive_read_needs_a_session_and_url_token_is_ignored(self):
        for path in ("/api/config", "/api/config/export", "/api/config/versions", "/api/groups", "/api/health",
                     "/api/audits", "/api/bindings", "/api/leaves", "/api/pending", "/api/credentials/status",
                     "/api/ownership", "/api/patrol", "/api/groups/90001/pipeline", "/custom.css"):
            with self.subTest(path=path):
                response = await self.client.get(path, params={"token": TOKEN})
                self.assertEqual(response.status_code, 401)

    async def test_every_mutator_checks_csrf_before_validation_or_side_effect(self):
        await self.login()
        self.client.headers.pop("X-CSRF-Token")
        for method, path in (
            ("POST", "/api/logout"), ("PUT", "/api/config"), ("POST", "/api/config/import"), ("POST", "/api/config/restore"),
            ("PUT", "/api/groups/90001"), ("DELETE", "/api/groups/90001"), ("PUT", "/api/groups/90001/pipeline"),
            ("POST", "/api/simulate"), ("POST", "/api/bindings/10001/clear"), ("DELETE", "/api/leaves/90001/10001"),
            ("POST", "/api/pending/1/resolve"), ("POST", "/api/credentials/qr"), ("POST", "/api/credentials/qr/poll"),
            ("POST", "/api/credentials/refresh"), ("POST", "/api/credentials/fingerprints"),
            ("POST", "/api/ownership/task-one/revoke"), ("POST", "/api/patrol/run"), ("POST", "/api/patrol/report-one/act"),
        ):
            with self.subTest(path=path):
                response = await self.client.request(method, path, json={})
                self.assertEqual(response.status_code, 403, response.text)
        self.credentials.start_qr_login.assert_not_awaited()
        self.patrol.act.assert_not_awaited()

    async def test_login_rate_limit_cookie_properties_and_logout(self):
        self.update(webui_login_max_attempts=2)
        self.assertEqual((await self.client.post("/api/login", json={"token": "bad"})).status_code, 401)
        self.assertEqual((await self.client.post("/api/login", json={"token": "bad"})).status_code, 401)
        self.assertEqual((await self.client.post("/api/login", json={"token": TOKEN})).status_code, 429)
        other = self.new_client(base="https://gatekeeper.example")
        self.extra_clients.append(other)
        other.headers["X-Forwarded-For"] = "203.0.113.8"
        # Untrusted forwarded addresses cannot evade per-peer login throttling.
        self.assertEqual((await other.post("/api/login", json={"token": TOKEN})).status_code, 429)

    async def test_successful_login_uses_http_only_strict_session_and_logout_revokes_it(self):
        response = await self.login()
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertNotIn(TOKEN, response.headers["set-cookie"])
        self.assertEqual((await self.client.get("/api/session")).status_code, 200)
        self.assertEqual((await self.client.post("/api/logout")).status_code, 200)
        self.assertEqual((await self.client.get("/api/config")).status_code, 401)

    async def test_cross_origin_mutations_are_refused_even_with_valid_session(self):
        await self.login()
        response = await self.client.post("/api/credentials/refresh", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.credentials.refresh_if_due.assert_not_awaited()

    async def test_https_and_loopback_proxy_trust_cannot_be_forged_by_remote_peer(self):
        self.update(webui_require_https=True, webui_trust_proxy_headers=True)
        remote = self.new_client(peer="198.51.100.20")
        self.extra_clients.append(remote)
        response = await remote.get("/admin", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status_code, 426)
        local = self.new_client(base="https://gatekeeper.example")
        self.extra_clients.append(local)
        response = await self.login(local)
        self.assertIn("__Host-gatekeeper_session", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("Strict-Transport-Security", response.headers)
        self.assertEqual((await local.get("/api/config")).status_code, 200)
        proxy = self.new_client()
        self.extra_clients.append(proxy)
        response = await proxy.get("/admin", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status_code, 200)

    async def test_disabled_or_publicly_bound_admin_does_not_affect_health_route(self):
        self.update(webui_enabled=False)
        self.assertEqual((await self.client.get("/api/config")).status_code, 404)
        self.assertEqual((await self.client.get("/healthz")).status_code, 200)
        self.update(webui_enabled=True)
        self.runtime.settings.host = "0.0.0.0"
        self.assertEqual((await self.client.get("/admin")).status_code, 503)

    async def test_expiry_and_local_token_rotation_invalidate_existing_sessions(self):
        await self.login()
        actual = time.monotonic()
        with patch("app.web.admin_api.time", SimpleNamespace(monotonic=lambda: actual + 999999, time=time.time)):
            self.assertEqual((await self.client.get("/api/config")).status_code, 401)
        await self.login()
        self.update(webui_token="replacement-token-" + "y" * 40)
        self.assertEqual((await self.client.get("/api/config")).status_code, 401)

    async def test_config_export_masks_secrets_and_cas_preserves_private_values(self):
        await self.login()
        snapshot = (await self.client.get("/api/config")).json()
        self.assertEqual(snapshot["config"]["bili_cookie"], "[redacted]")
        self.assertEqual(snapshot["config"]["webui_token"], "[redacted]")
        exported = await self.client.get("/api/config/export?include_secrets=true")
        for secret in (TOKEN, "private-cookie", "private-refresh", "private-smtp"):
            self.assertNotIn(secret, exported.text)
        snapshot["config"]["groups"]["90001"]["required_qq_level"] = 16
        payload = {"config": snapshot["config"], "expected_version": snapshot["version"]}
        response = await self.client.put("/api/config", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(ConfigManager.get_bili_cookie(), "SESSDATA=private-cookie")
        self.assertEqual(ConfigManager.get_config().bili_refresh_token, "private-refresh")
        self.assertEqual((await self.client.put("/api/config", json=payload)).status_code, 409)

    async def test_web_admin_cannot_change_its_token_or_listener_trust(self):
        await self.login()
        snapshot = (await self.client.get("/api/config")).json()
        for field, value in (("webui_token", "replacement-" + "z" * 40), ("webui_host", "0.0.0.0"),
                             ("webui_trust_proxy_headers", True)):
            data = json.loads(json.dumps(snapshot["config"]))
            data[field] = value
            response = await self.client.put("/api/config", json={"config": data, "expected_version": snapshot["version"]})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(ConfigManager.get_config().webui_token, TOKEN)

    async def test_large_uids_round_trip_without_javascript_number_rounding(self):
        value = 123456789012345678
        config = ConfigManager.get_config().model_copy(deep=True)
        config.groups["90001"].target_uids = [value]
        ConfigManager.replace_config(config, ConfigManager.get_version(), actor="large-id-test")
        await self.login()
        snapshot = (await self.client.get("/api/config")).json()
        self.assertEqual(snapshot["config"]["groups"]["90001"]["target_uids"], [str(value)])
        response = await self.client.put("/api/config", json={"config": snapshot["config"], "expected_version": snapshot["version"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(ConfigManager.get_group_config("90001").target_uids, [value])
        self.assertEqual((await self.client.get("/api/audits?bili_uid=" + "9" * 30)).status_code, 422)

    async def test_yaml_import_versions_and_restore_create_revision_without_losing_credentials(self):
        await self.login()
        text = (await self.client.get("/api/config/export")).text
        payload = yaml.safe_load(text)
        payload["config"]["groups"]["90001"]["required_bili_level"] = 4
        response = await self.client.post("/api/config/import", json={"expected_version": self.version, "yaml": yaml.safe_dump(payload)})
        self.assertEqual(response.status_code, 200, response.text)
        changed = response.json()["version"]
        self.assertEqual(ConfigManager.get_group_config("90001").required_bili_level, 4)
        versions = (await self.client.get("/api/config/versions")).json()["versions"]
        self.assertEqual(versions[0]["version"], changed)
        response = await self.client.post("/api/config/restore", json={"expected_version": changed, "version": self.version})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreater(response.json()["version"], changed)
        self.assertEqual(ConfigManager.get_group_config("90001").required_bili_level, 0)
        self.assertEqual(ConfigManager.get_bili_cookie(), "SESSDATA=private-cookie")

    async def test_group_and_expression_pipeline_edit_use_same_sql_revision(self):
        await self.login()
        snapshot = (await self.client.get("/api/groups")).json()
        group = snapshot["groups"]["90001"]
        group["required_qq_level"] = 16
        response = await self.client.put("/api/groups/90001", json={"expected_version": snapshot["version"], "group": group})
        self.assertEqual(response.status_code, 200, response.text)
        version = response.json()["version"]
        pipeline = {"version": 3, "entry": "first", "groups": [{
            "id": "first", "name": "数值表达式", "when": "qq_level >= 16 and (follow or medal_level >= 10)",
            "on_true": "approve", "on_false": {"flow": "reject", "reason": "群规则"}, "on_unknown": "ignore",
        }]}
        response = await self.client.put("/api/groups/90001/pipeline", json={"expected_version": version, "pipeline": pipeline})
        self.assertEqual(response.status_code, 200, response.text)
        group = ConfigManager.get_group_config("90001")
        self.assertEqual(group.review_stages[0].when, pipeline["groups"][0]["when"])
        self.assertEqual(group.review_stages[0].conditions, ["qq_level", "follow", "medal"])
        response = await self.client.get("/api/groups/90001/pipeline")
        self.assertEqual(response.json()["pipeline"]["version"], 3)
        self.assertIn("medal_level >= 10", response.json()["yaml"])
        self.assertEqual((await self.client.put("/api/groups/90001/pipeline", json={"expected_version": version, "pipeline": pipeline})).status_code, 409)

    async def test_invalid_pipeline_does_not_change_sql_or_live_source_files(self):
        await self.login()
        response = await self.client.put("/api/groups/90001/pipeline", json={
            "expected_version": self.version, "yaml": "version: 3\ngroups:\n - id: bad\n   when: __import__('os')\n",
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(ConfigManager.get_version(), self.version)
        self.assertEqual(list(self.root.glob("review*.yaml")), [])

    async def test_simulation_is_explicit_and_does_not_create_approval_audit(self):
        await self.login()
        response = await self.client.post("/api/simulate", json={"group_id": "90001", "answer": "UID: 88", "qq": "10001"})
        self.assertEqual(response.status_code, 200)
        self.service.simulate.assert_awaited_once_with("90001", "UID: 88", qq="10001")
        self.assertEqual(Records.get_pending_decision_count(), 0)

    async def test_health_reports_actual_disk_limiter_and_cookie_scoped_target_readiness(self):
        config = ConfigManager.get_config().model_copy(deep=True)
        config.groups["90001"].target_uids = [2, 3]
        ConfigManager.replace_config(config, ConfigManager.get_version(), actor="health-setup")
        coordinator = BiliRequestCoordinator()
        self.service.bili_requests = coordinator
        coordinator.set_verified_common_targets(config.bili_cookie, {2})
        await self.login()
        response = await self.client.get("/api/health")
        data = response.json()
        self.assertEqual(data["database"]["disk"]["state"], "known")
        self.assertGreater(data["database"]["disk"]["free_bytes"], 0)
        self.assertIn("waiting", data["bilibili"]["metrics"])
        self.assertIn("requests_per_second", data["bilibili"]["metrics"])
        self.assertEqual(data["bilibili"]["common_targets"], [{"uid": 2, "verified": True}, {"uid": 3, "verified": False}])
        self.assertFalse(data["bilibili"]["all_targets_verified"])
        self.assertNotIn("private-cookie", response.text)
        self.update(bili_cookie="SESSDATA=replacement")
        data = (await self.client.get("/api/health")).json()
        self.assertFalse(data["bilibili"]["common_targets"][0]["verified"])

    async def test_record_filters_binding_release_and_pending_guard(self):
        Records.bind_identity("10001", "昵称<script>", 88, "90001")
        Records.add_audit(group_id="90001", qq="10001", result="ignored", reasons=["未满足要求"])
        LeaveRecordManager.add_leave_record("90001", "10001", "昵称")
        await self.login()
        self.assertEqual(len((await self.client.get("/api/audits?qq=10001")).json()["items"]), 1)
        self.assertEqual((await self.client.get("/api/audits", params={"qq": "10001' OR 1=1 --"})).json()["items"], [])
        self.assertEqual((await self.client.get("/api/bindings?bili_uid=88")).json()["items"][0]["owner_qq"], "10001")
        self.assertEqual((await self.client.get("/api/leaves?group_id=90001")).json()["items"][0]["qq"], "10001")
        audit = Records.begin_pending_decision(group_id="90001", qq="10001", bili_uid=88, result="approved", reasons=[], request_key="specific")
        response = await self.client.post("/api/bindings/10001/clear", json={"confirm_qq": "10001"})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(Records.get_bili_binding(88))
        self.assertEqual((await self.client.post(f"/api/pending/{audit.id}/resolve", json={"applied": False})).status_code, 422)
        response = await self.client.post(f"/api/pending/{audit.id}/resolve", json={"applied": False, "confirmed": True})
        self.assertEqual(response.status_code, 200)
        response = await self.client.post("/api/bindings/10001/clear", json={"confirm_qq": "10001"})
        self.assertEqual(response.json()["removed_uids"], [88])
        self.assertEqual((await self.client.delete("/api/leaves/90001/10001")).json(), {"removed": True})

    async def test_qr_login_is_bound_to_admin_session_and_uses_local_svg(self):
        await self.login()
        response = await self.client.post("/api/credentials/qr")
        self.assertEqual(response.status_code, 200, response.text)
        image = await self.client.get(response.json()["image"])
        self.assertEqual(image.status_code, 200)
        self.assertIn("image/svg+xml", image.headers["content-type"])
        self.assertIn("<svg", image.text)
        another = self.new_client()
        self.extra_clients.append(another)
        await self.login(another)
        self.assertEqual((await another.get(response.json()["image"])).status_code, 404)
        self.assertEqual((await another.post("/api/credentials/qr/poll", json={"session_id": "qr-test-id"})).status_code, 404)
        polled = await self.client.post("/api/credentials/qr/poll", json={"session_id": "qr-test-id"})
        self.assertEqual(polled.json(), {"state": "success"})
        self.credentials.poll_qr_login.assert_awaited_once_with("qr-test-id")

    async def test_credential_and_patrol_actions_are_explicit_and_validated(self):
        await self.login()
        self.assertEqual((await self.client.get("/api/credentials/status")).json(), {"state": "valid"})
        self.assertEqual((await self.client.post("/api/credentials/refresh")).json(), {"state": "not_due"})
        self.assertEqual((await self.client.post("/api/credentials/fingerprints")).json(), {"state": "present"})
        self.assertEqual((await self.client.get("/api/ownership")).json()["items"][0]["uid"], 88)
        self.assertTrue((await self.client.post("/api/ownership/task-one/revoke")).json()["revoked"])
        self.assertEqual((await self.client.get("/api/patrol")).json()["status"]["default_mode"], "report")
        self.assertEqual((await self.client.post("/api/patrol/run", json={"group_id": "90001"})).status_code, 200)
        self.assertEqual((await self.client.post("/api/patrol/report-one/act", json={"action": "kick"})).status_code, 422)
        self.patrol.act.assert_not_awaited()
        response = await self.client.post("/api/patrol/report-one/act", json={"action": "remind", "confirmed": True, "qq": "10001"})
        self.assertEqual(response.status_code, 200)
        self.patrol.act.assert_awaited_once_with("report-one", "remind", qq="10001")

    async def test_bad_credentials_url_or_oversized_payload_are_not_returned_or_applied(self):
        await self.login()
        self.credentials.start_qr_login.return_value["url"] = "javascript:alert(1)"
        self.assertEqual((await self.client.post("/api/credentials/qr")).status_code, 502)
        response = await self.client.put("/api/config", content=b"x" * (1024 * 1024 + 1), headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(ConfigManager.get_version(), self.version)

    async def test_recursive_yaml_and_deep_json_fail_before_config_writes(self):
        await self.login()
        recursive = "version: 3\nconfig: &root\n  groups: *root\n"
        response = await self.client.post("/api/config/import", json={"expected_version": self.version, "yaml": recursive})
        self.assertEqual(response.status_code, 422)
        response = await self.client.put("/api/groups/90001/pipeline", json={
            "expected_version": self.version, "yaml": "version: 3\ngroups: &root [*root]\n",
        })
        self.assertEqual(response.status_code, 422)
        deeply_nested = '{"config":' + '[' * 40 + '0' + ']' * 40 + '}'
        response = await self.client.put("/api/config", content=deeply_nested, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ConfigManager.get_version(), self.version)


if __name__ == "__main__":
    unittest.main()
