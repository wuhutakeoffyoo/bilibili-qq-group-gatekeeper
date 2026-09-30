"""Regression tests for review decisions and live configuration updates."""

import http.client
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.group_request_manager import config as config_module
from src.plugins.group_request_manager import main as manager_main
from src.plugins.group_request_manager.bili_api import BiliApi, CheckResult
from src.plugins.group_request_manager.config import (
    ConfigManager,
    GroupConfig,
    PluginConfig,
    load_group_configs_from_yaml_file,
)
from src.plugins.group_request_manager.main import JoinRequestContext, _build_rule_condition_statuses
from src.plugins.group_request_manager.webui import _resolve_pipeline_file, start_webui


PIPELINE = """\
version: 2
entry: only
groups:
  - id: only
    name: only
    mode: all_pass
    conditions: [bili_account]
    on_true: {flow: approve}
    on_false: {flow: reject}
    on_unknown: {flow: ignore}
"""


class IdentityDecisionTests(unittest.TestCase):
    def _statuses(self, search_result: CheckResult, **kwargs):
        return _build_rule_condition_statuses(
            GroupConfig(group_id="1"),
            {"no_conflict", "no_identity_change"},
            qq_level=None,
            bili_search_result=search_result,
            bili_level=None,
            follow_result=CheckResult("not_required"),
            medal_result=CheckResult("not_required"),
            leave_record=None,
            conflict_qq=None,
            owned_other_bili_uids=[],
            **kwargs,
        )

    def test_missing_bili_identity_cannot_pass_binding_checks(self):
        statuses = self._statuses(CheckResult("inaccessible"))
        self.assertEqual({value.state for value in statuses.values()}, {"unknown"})

        invalid_uid_statuses = self._statuses(CheckResult("passed", matched_target_uid=0))
        self.assertEqual({value.state for value in invalid_uid_statuses.values()}, {"unknown"})

    def test_failed_binding_queries_cannot_pass(self):
        statuses = self._statuses(
            CheckResult("passed", matched_target_uid=123),
            conflict_lookup_known=False,
            identity_change_lookup_known=False,
        )
        self.assertEqual({value.state for value in statuses.values()}, {"unknown"})

    def test_completed_binding_queries_can_pass(self):
        statuses = self._statuses(
            CheckResult("passed", matched_target_uid=123),
            conflict_lookup_known=True,
            identity_change_lookup_known=True,
        )
        self.assertEqual({value.state for value in statuses.values()}, {"true"})


class IdentityLookupFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_lookup_error_does_not_become_known_clean_binding(self):
        config = GroupConfig(group_id="1")
        context = JoinRequestContext(
            group_id="1", user_id="10001", group_config=config,
            actor_qq_for_binding="10001", bili_name="example",
        )
        api = SimpleNamespace(search_user_profile=AsyncMock(return_value=CheckResult(
            "passed", matched_target_uid=123, matched_level=5,
        )))

        async def run_bili(_cookie, operation):
            return await operation(api)

        with (
            patch.object(manager_main._bili_requests, "run", side_effect=run_bili),
            patch.object(manager_main, "run_storage", side_effect=RuntimeError("database unavailable")),
        ):
            with self.assertRaises(RuntimeError):
                await manager_main._populate_bili_context(
                    context, config, {"no_conflict", "no_identity_change"}, "test-cookie"
                )

        self.assertEqual(context.bili_search_result.state, "passed")
        self.assertFalse(context.conflict_lookup_known)
        self.assertFalse(context.identity_change_lookup_known)


class MedalCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_medal_level_is_inaccessible(self):
        api = BiliApi(client=object())
        api._get = AsyncMock(return_value={
            "code": 0,
            "data": {"list": [{"medal_info": {"target_id": 200000003}}]},
        })
        result = await api.check_medal_any_status(123, [200000003], 1)
        self.assertEqual(result.state, "inaccessible")

    async def test_valid_target_medal_still_passes_with_another_invalid_entry(self):
        api = BiliApi(client=object())
        api._get = AsyncMock(return_value={
            "code": 0,
            "data": {"list": [
                {"medal_info": {"target_id": 200000003, "level": 1}},
                {"medal_info": {"target_id": 456}},
            ]},
        })
        result = await api.check_medal_any_status(123, [200000003], 1)
        self.assertEqual(result.state, "passed")


class RuntimeConfigTests(unittest.TestCase):
    def test_state_file_is_private_when_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_state.json"
            config = PluginConfig(bili_cookie="test-cookie-value")
            with (
                patch.object(config_module, "CONFIG_FILE", path),
                patch.object(ConfigManager, "_config", config),
            ):
                ConfigManager._save_config()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["bili_cookie"], "test-cookie-value")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode semantics required")
    def test_existing_state_file_permissions_are_tightened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_state.json"
            path.write_text('{"bili_cookie":"test-cookie-value"}', encoding="utf-8")
            path.chmod(0o644)
            ConfigManager._restrict_state_permissions(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_restart_uses_current_pipeline_yaml_instead_of_cached_json_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline_file = Path(directory) / "review_pipeline.yaml"
            pipeline_file.write_text(
                PIPELINE.replace("on_true: {flow: approve}", "on_true: {flow: reject}"),
                encoding="utf-8",
            )
            stale = GroupConfig(
                group_id="123",
                review_pipeline_file=str(pipeline_file),
                review_stages=[
                    config_module.ReviewStageConfig(
                        index=1,
                        conditions=["bili_account"],
                        on_true=["approve"],
                    )
                ],
            )
            config = PluginConfig(groups={"123": stale})
            ConfigManager._refresh_review_stages(config)
            self.assertEqual(config.groups["123"].review_stages[0].on_true, ["reject"])


class WebUIPipelineTests(unittest.TestCase):
    token = "test-token-with-at-least-32-characters"

    def test_nested_groups_file_and_http_save_update_active_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "configs"
            config_dir.mkdir()
            pipeline_file = config_dir / "review_pipeline.yaml"
            pipeline_file.write_text(PIPELINE, encoding="utf-8")
            groups_file = config_dir / "groups.yaml"
            groups_file.write_text(
                "groups:\n"
                "  '123':\n    review_pipeline_file: review_pipeline.yaml\n"
                "  '456':\n    review_pipeline_file: review_pipeline.yaml\n",
                encoding="utf-8",
            )
            groups = load_group_configs_from_yaml_file(groups_file)
            group = groups[0]
            config = PluginConfig(
                groups={item.group_id: item for item in groups},
                webui_host="127.0.0.1",
                webui_port=0,
                webui_token=self.token,
            )
            self.assertEqual(_resolve_pipeline_file(config), pipeline_file)
            self.assertEqual(group.review_stages[0].on_true, ["approve"])

            server = start_webui(config)
            self.assertIsNotNone(server)
            assert server is not None
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                connection.request(
                    "POST", "/api/login", json.dumps({"token": self.token}),
                    {"Content-Type": "application/json"},
                )
                login = connection.getresponse()
                self.assertEqual(login.status, 200)
                cookie = login.getheader("Set-Cookie").split(";", 1)[0]
                csrf = json.loads(login.read())["csrf"]

                revised = PIPELINE.replace("on_true: {flow: approve}", "on_true: {flow: reject}")
                connection.request(
                    "PUT", "/api/pipeline", json.dumps({"yaml": revised}),
                    {
                        "Content-Type": "application/json",
                        "Cookie": cookie,
                        "X-CSRF-Token": csrf,
                    },
                )
                response = connection.getresponse()
                response_body = response.read()
                self.assertEqual(response.status, 200, response_body)
                self.assertEqual(group.review_stages[0].on_true, ["reject"])
                self.assertEqual(config.groups["456"].review_stages[0].on_true, ["reject"])
                self.assertEqual(pipeline_file.read_text(encoding="utf-8"), revised)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
