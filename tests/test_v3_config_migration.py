"""The legacy sources compile once; SQLite owns every later configuration."""
from __future__ import annotations

import json
import base64
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from app.config import ConfigManager, ConfigVersionConflict, GroupConfig, REDACTED, load_review_stages_from_yaml_file
from app.database.repository import ConfigRepository
from app.database.legacy import database_connection
from app.settings import load_settings


PIPELINE = """version: 2
groups:
  entry:
    conditions: [bili_account, bili_level]
    mode: all_pass
    on_true: approve
    on_false: reject
    on_unknown: ignore
"""


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.settings = SimpleNamespace(app_root=self.root)
        ConfigManager.reset_state()

    def tearDown(self):
        ConfigManager.reset_state()
        self.environment.stop()
        self.directory.cleanup()

    def write(self, path, value):
        selected = self.root / path
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text(value, encoding="utf-8")
        return selected

    def initialize(self):
        return ConfigManager.initialize(self.settings)

    def test_complete_legacy_precedence_and_nested_pipeline_paths(self):
        pipeline = self.write("config/nested/rules/custom.yaml", PIPELINE)
        self.write("config/nested/groups.yaml", yaml.safe_dump({
            "defaults": {"required_qq_level": 2, "required_bili_level": 2, "review_pipeline_file": "rules/custom.yaml"},
            "groups": {"100": {"target_uids": [2]}, "101": {"target_uids": [101]}},
        }))
        self.write("data/runtime/plugin_state.json", json.dumps({
            "bili_cookie": "runtime-cookie", "admin_qqs": ["runtime-admin"],
            "groups": {"100": {"group_id": "100", "required_qq_level": 1, "required_bili_level": 1}},
        }))
        self.write("data/config/plugin_config.json", json.dumps({"groups": {"888": {"group_id": "888"}}}))
        self.write("data/runtime/group_overrides.json", json.dumps({"groups": {
            "100": {"required_qq_level": 3}, "102": {"required_qq_level": 3},
        }}))
        self.settings.group_config_file = "config/nested/groups.yaml"
        self.settings.group_configs = json.dumps({"100": {
            "required_qq_level": 4, "required_bili_level": 4, "target_uids": [4],
            "review_pipeline_file": str(pipeline),
        }})
        self.settings.group_id = "100"
        self.settings.group_required_qq_level = "5"
        self.settings.group_required_bili_level = "5"
        self.settings.group_target_uids = "5"
        self.settings.group_1_id = "100"
        self.settings.group_1_required_qq_level = "6"
        self.settings.group_2_id = "200"
        self.settings.group_2_required_qq_level = "2"
        self.settings.group_4_id = "400"  # Legacy numbering intentionally stops at the first gap.
        self.settings.bili_cookie = "effective-env-cookie"
        self.settings.admin_qqs = "admin-one,admin-two"
        self.settings.notify_admins_on_abnormal_request = "true"
        self.settings.webui_enabled = "true"
        config = self.initialize()
        self.assertEqual(set(config.groups), {"100", "101", "102", "200"})
        self.assertEqual(config.groups["100"].required_qq_level, 6)
        self.assertEqual(config.groups["100"].required_bili_level, 5)
        self.assertEqual(config.groups["100"].target_uids, [5])
        self.assertEqual(config.groups["101"].review_pipeline_file, str(pipeline.resolve()))
        self.assertEqual(config.groups["101"].review_stages[0].conditions, ["bili_account", "bili_level"])
        self.assertEqual(config.groups["102"].required_qq_level, 3)
        self.assertEqual(config.bili_cookie, "effective-env-cookie")
        self.assertEqual(config.admin_qqs, ["admin-one", "admin-two"])
        self.assertEqual(config.notify_admins_on_abnormal_request, "all")
        self.assertTrue(config.webui_enabled)
        row = ConfigRepository.current()
        manifest = json.loads(row["manifest_json"])
        self.assertIn(pipeline.resolve(), {Path(entry["path"]).resolve() for entry in manifest["files"]})
        backup = Path(manifest["backup"])
        self.assertTrue(backup.is_file())
        if os.name == "posix":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_restart_stops_merging_legacy_and_retains_compiled_pipeline(self):
        pipeline = self.write("nested/review.yaml", PIPELINE)
        self.settings.group_id = "100"
        self.settings.group_required_qq_level = "6"
        self.settings.group_review_pipeline_file = str(pipeline)
        self.settings.bili_cookie = "initial-cookie"
        self.initialize()
        ConfigManager.set_group_config(GroupConfig(group_id="100", required_qq_level=20))
        ConfigManager.set_credentials("manual-cookie", "manual-token")
        ConfigManager.reset_state()
        self.settings.group_required_qq_level = "999"
        self.settings.bili_cookie = "stale-env-cookie"
        pipeline.unlink()
        config = self.initialize()
        self.assertEqual(config.groups["100"].required_qq_level, 20)
        self.assertEqual(config.bili_cookie, "manual-cookie")
        self.assertEqual(config.bili_refresh_token, "manual-token")
        self.assertEqual(ConfigManager.get_version(), 3)

    def test_manifest_keeps_superseded_yaml_sources_and_exact_source_bytes(self):
        initial = self.write("nested/initial.yaml", PIPELINE)
        final = self.write("final.yaml", PIPELINE.replace("bili_level", "qq_level"))
        groups = self.write("nested/groups.yaml", "version: 1\ndefaults:\n  review_pipeline_file: initial.yaml\ngroups:\n  '100': {}\n")
        groups.write_bytes(b"\xef\xbb\xbf" + groups.read_bytes().replace(b"\n", b"\r\n"))
        self.settings.group_config_file = str(groups)
        self.settings.group_id = "100"
        self.settings.group_review_pipeline_file = str(final)
        self.initialize()
        manifest = json.loads(ConfigRepository.current()["manifest_json"])
        sources = {row["path"]: row for row in manifest["files"]}
        self.assertIn(str(initial.resolve()), sources)
        self.assertIn(str(final.resolve()), sources)
        self.assertEqual(base64.b64decode(sources[str(groups.resolve())]["raw_base64"]), groups.read_bytes())

    def test_empty_sql_cookie_does_not_fall_back_to_environment(self):
        self.settings.bili_cookie = "old-cookie"
        self.initialize()
        ConfigManager.set_credentials("")
        ConfigManager.reset_state()
        self.initialize()
        self.assertEqual(ConfigManager.get_bili_cookie(), "")

    def test_legacy_file_fallback_and_nonempty_runtime_cookie(self):
        self.write("data/config/plugin_config.json", json.dumps({"bili_cookie": "legacy-cookie", "admin_qqs": ["123"]}))
        self.assertEqual(self.initialize().bili_cookie, "legacy-cookie")
        self.assertEqual(ConfigManager.get_admin_qqs(), ["123"])

    def test_dotenv_environment_file_and_process_precedence(self):
        self.write(".env", "ENVIRONMENT=qa\nBILI_COOKIE=base-cookie\nADMIN_QQS=base-admin\n")
        self.write(".env.qa", "BILI_COOKIE=qa-cookie\nADMIN_QQS=qa-admin\n")
        self.write("data/runtime/plugin_state.json", json.dumps({"bili_cookie": "runtime-cookie"}))
        os.environ["BILI_COOKIE"] = "process-cookie"
        config = ConfigManager.initialize(load_settings(self.root, publish_environment=False))
        self.assertEqual(config.bili_cookie, "process-cookie")
        self.assertEqual(config.admin_qqs, ["qa-admin"])

    def test_smtp_and_cookie_monitor_settings_are_imported_once_and_redacted(self):
        self.settings.notify_smtp_host = "smtp.example.test"
        self.settings.notify_smtp_port = "465"
        self.settings.notify_smtp_user = "sender@example.test"
        self.settings.notify_smtp_password = "private-smtp-secret"
        self.settings.notify_from_addr = "sender@example.test"
        self.settings.notify_to_addr = "recipient@example.test"
        self.settings.cookie_check_interval_seconds = "600"
        config = self.initialize()
        self.assertEqual(config.notify_smtp_password, "private-smtp-secret")
        self.assertEqual(config.cookie_check_interval_seconds, "600")
        self.assertEqual(ConfigManager.get_public_config()["notify_smtp_password"], REDACTED)
        manifest = json.loads(ConfigRepository.current()["manifest_json"])
        self.assertEqual(manifest["effective_settings"]["notify_smtp_password"], "private-smtp-secret")
        self.settings.notify_smtp_password = "stale-env-secret"
        self.settings.cookie_check_interval_seconds = "999"
        ConfigManager.reset_state()
        config = self.initialize()
        self.assertEqual(config.notify_smtp_password, "private-smtp-secret")
        self.assertEqual(config.cookie_check_interval_seconds, "600")

    def test_conflict_restore_and_append_only_history(self):
        self.initialize()
        version_one = ConfigManager.get_version()
        modified = ConfigManager.get_config().model_copy(deep=True)
        modified.groups["1"] = GroupConfig(group_id="1", required_qq_level=10)
        version_two = ConfigManager.replace_config(modified, version_one, "operator")
        with self.assertRaises(ConfigVersionConflict):
            ConfigManager.replace_config(modified, version_one)
        version_three = ConfigManager.restore_version(version_one, version_two, "rollback")
        self.assertGreater(version_three, version_two)
        self.assertNotIn("1", ConfigManager.get_config().groups)
        history = ConfigManager.list_versions()
        self.assertEqual([entry["version"] for entry in history], [3, 2, 1])
        self.assertEqual(history[0]["restored_from"], 1)
        self.assertEqual(json.loads(ConfigRepository.get(2)["config_json"])["groups"]["1"]["required_qq_level"], 10)
        self.assertNotIn("config_json", history[0])
        self.assertNotIn("manifest_json", history[0])
        with database_connection() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE config_revisions SET actor='changed' WHERE version=1")
        with database_connection() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM config_revisions WHERE version=1")

    def test_credentials_generation_cas_and_redaction(self):
        self.settings.bili_cookie = "initial-cookie"
        self.settings.smtp_password = "smtp-secret"
        self.settings.cookiecloud_enabled = "true"
        self.initialize()
        changed = ConfigManager.set_credentials("new-cookie", "refresh-secret", expected_generation=1)
        self.assertEqual(changed["generation"], 2)
        with self.assertRaises(ConfigVersionConflict):
            ConfigManager.set_credentials("stale-cookie", "old-secret", expected_generation=1)
        config = ConfigManager.get_config().model_copy(deep=True)
        config.bili_refresh_pending = {"old_refresh_token": "original-secret", "cookie": "pending-secret"}
        ConfigManager.replace_config(config, changed["version"], actor="credential-service")
        snapshot = ConfigManager.get_public_config()
        self.assertEqual(snapshot["bili_cookie"], REDACTED)
        self.assertEqual(snapshot["bili_refresh_token"], REDACTED)
        self.assertEqual(snapshot["bili_refresh_pending"], REDACTED)
        self.assertEqual(snapshot["smtp_password"], REDACTED)
        self.assertEqual(snapshot["cookiecloud_enabled"], "true")
        self.assertEqual(ConfigManager.get_bili_cookie(), "new-cookie")

    def test_general_replacement_advances_generation_and_discards_stale_session_state(self):
        self.initialize()
        ConfigManager.set_credentials("old-cookie", "old-token")
        snapshot = ConfigManager.get_snapshot(include_secrets=True)
        candidate = snapshot["config"]
        candidate["bili_cookie"] = "different-cookie"
        candidate["credential_generation"] = 0
        candidate["bili_refresh_pending"] = {"old_refresh_token": "resurrected-token"}
        ConfigManager.replace_config(candidate, snapshot["version"], actor="config-import")
        current = ConfigManager.get_config()
        self.assertEqual(current.credential_generation, 2)
        self.assertEqual(current.bili_refresh_token, "")
        self.assertEqual(current.bili_refresh_pending, {})

    def test_rule_edits_cannot_inject_pending_or_reset_live_generation(self):
        self.initialize()
        ConfigManager.set_credentials("live-cookie", "live-token")
        snapshot = ConfigManager.get_snapshot(include_secrets=True)
        candidate = snapshot["config"]
        candidate["bili_refresh_pending"] = {"old_refresh_token": "valid-old-token"}
        ConfigManager.replace_config(candidate, snapshot["version"], actor="credential-service")
        snapshot = ConfigManager.get_snapshot(include_secrets=True)
        candidate = snapshot["config"]
        candidate["admin_qqs"] = ["42"]
        candidate["credential_generation"] = 0
        candidate["bili_refresh_pending"] = {"old_refresh_token": "injected-historical-token"}
        ConfigManager.replace_config(candidate, snapshot["version"], actor="admin-edit")
        current = ConfigManager.get_config()
        self.assertEqual(current.credential_generation, 2)
        self.assertEqual(current.admin_qqs, ["42"])
        self.assertEqual(current.bili_refresh_pending, {"old_refresh_token": "valid-old-token"})

    def test_restore_reverts_business_fields_and_preserves_live_credential_bundle(self):
        self.initialize()
        ConfigManager.set_credentials("historical-cookie", "historical-token")
        old_version = ConfigManager.get_version()
        ConfigManager.set_group_config(GroupConfig(group_id="123", required_bili_level=6))
        ConfigManager.set_credentials("live-cookie", "live-token")
        snapshot = ConfigManager.get_snapshot(include_secrets=True)
        snapshot["config"]["bili_refresh_pending"] = {"old_refresh_token": "live-pending-confirmation"}
        ConfigManager.replace_config(snapshot["config"], snapshot["version"], actor="credential-service")
        expected = ConfigManager.get_config().model_dump()
        ConfigManager.restore_version(old_version, ConfigManager.get_version(), actor="operator")
        restored = ConfigManager.get_config()
        self.assertNotIn("123", restored.groups)
        for key in ("bili_cookie", "bili_refresh_token", "credential_generation", "bili_refresh_pending"):
            self.assertEqual(getattr(restored, key), expected[key])

    def test_compat_setters_keep_unrelated_external_process_edits(self):
        self.initialize()
        ConfigManager.set_group_config(GroupConfig(group_id="123", required_qq_level=1, required_bili_level=1))
        stale_group = ConfigManager.get_group_config("123").model_copy(deep=True)
        stale_group.required_qq_level = 20
        # A separate SQLite writer is independent of the process-local cache.
        external = json.loads(ConfigRepository.current()["config_json"])
        external["groups"]["123"]["required_bili_level"] = 6
        external["bili_cookie"] = "externally-updated-cookie"
        external["bili_refresh_token"] = "externally-updated-token"
        external["credential_generation"] = 9
        ConfigRepository.append(external, expected_version=2, actor="external-process")
        ConfigManager.set_group_config(stale_group, ["required_qq_level"])
        current = ConfigManager.get_config()
        self.assertEqual(current.groups["123"].required_qq_level, 20)
        self.assertEqual(current.groups["123"].required_bili_level, 6)
        self.assertEqual(current.bili_cookie, "externally-updated-cookie")
        ConfigManager.set_bili_cookie("manual-cookie")
        current = ConfigManager.get_config()
        self.assertEqual(current.bili_cookie, "manual-cookie")
        self.assertEqual(current.bili_refresh_token, "")
        self.assertEqual(current.bili_refresh_pending, {})
        self.assertEqual(current.credential_generation, 10)

    def test_cas_prevents_change_between_read_and_sql_commit(self):
        self.initialize()
        append = ConfigRepository.append
        def concurrent_append(config, **kwargs):
            external = json.loads(ConfigRepository.current()["config_json"])
            external["bili_cookie"] = "external-winner"
            external["smtp_password"] = "external-secret"
            append(external, expected_version=kwargs["expected_version"], actor="external-process")
            return append(config, **kwargs)
        with patch.object(ConfigRepository, "append", side_effect=concurrent_append):
            with self.assertRaises(ConfigVersionConflict):
                ConfigManager.set_group_config(GroupConfig(group_id="123"))
        current = ConfigManager.get_config()
        self.assertEqual(current.bili_cookie, "external-winner")
        self.assertEqual(current.smtp_password, "external-secret")
        self.assertNotIn("123", current.groups)

    def test_yaml_roundtrip_preserves_extensions_compiled_stages_and_redacted_secrets(self):
        self.initialize()
        config = ConfigManager.get_config().model_copy(deep=True)
        config.bili_cookie = "protected-cookie"
        config.bili_refresh_token = "protected-refresh"
        config.patrol = {"interval_seconds": 300, "enabled": True}
        pipeline = self.write("rules.yaml", PIPELINE)
        config.groups["123"] = GroupConfig(group_id="123", review_stages=load_review_stages_from_yaml_file(pipeline))
        ConfigManager.replace_config(config, 1)
        original = ConfigManager.get_config().model_dump()
        public_text = ConfigManager.export_yaml()
        self.assertNotIn("protected-cookie", public_text)
        self.assertNotIn("protected-refresh", public_text)
        ConfigManager.import_yaml(public_text, 2, "redacted-import")
        self.assertEqual(ConfigManager.get_config().model_dump(), original)
        secret_text = ConfigManager.export_yaml(include_secrets=True)
        ConfigManager.import_yaml(secret_text, 3, "explicit-secret-import")
        self.assertEqual(ConfigManager.get_config().model_dump(), original)

    def test_corrupt_source_is_preserved_and_import_stops(self):
        source = self.write("data/runtime/plugin_state.json", "{ broken source")
        with self.assertRaisesRegex(ValueError, "停止首次迁移"):
            self.initialize()
        self.assertEqual(source.read_text(encoding="utf-8"), "{ broken source")
        self.assertIsNone(ConfigRepository.current())

    def test_v3_expression_yaml_is_compiled_and_preserved(self):
        source = self.write("when.yaml", "version: 3\ngroups:\n  - id: gate\n    when: 'bili_level >= 4 and qq_level >= 10'\n    on_true: approve\n")
        stage = load_review_stages_from_yaml_file(source)[0]
        self.assertEqual(stage.when, "bili_level >= 4 and qq_level >= 10")
        self.assertEqual(stage.conditions, ["bili_level", "qq_level"])

    def test_default_modules_import_without_nonebot(self):
        script = """import builtins, sys
old_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'nonebot' or name.startswith('nonebot.'):
        raise AssertionError('NoneBot must not be imported: ' + name)
    return old_import(name, *args, **kwargs)
builtins.__import__ = guarded
import app.config, app.database.legacy, app.database.records, app.database.leaves, app.reports
assert not any(k == 'nonebot' or k.startswith('nonebot.') for k in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
