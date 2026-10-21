"""Schema-3 preservation and actual transaction tests for framework-free storage."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.config import ConfigManager
from app.database import legacy
from app.database.leaves import LeaveRecordManager
from app.database.records import JoinRequestRecordManager
from app.database.repository import AdminRecordRepository, RuntimeStateRepository


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "data/records/gatekeeper.sqlite3"
        self.path.parent.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        ConfigManager.reset_state()
        legacy.configure_database(self.path, root=self.root)

    def tearDown(self):
        ConfigManager.reset_state()
        legacy.reset_database_state()
        self.environment.stop()
        self.directory.cleanup()

    def test_v3_upgrade_preserves_every_business_column_row_and_pending_intent(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        for version in (1, 2, 3):
            legacy.SCHEMA_MIGRATIONS[version](connection)
        connection.execute("PRAGMA user_version = 3")
        connection.execute("INSERT INTO metadata VALUES (?,?)", (legacy.MIGRATION_MARKER, '{"historical":true}'))
        connection.execute("INSERT INTO user_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            "123", "中文昵称", 456, "first", "last", '["group"]', '[456,789]', '["中文昵称"]', 3, 1, 1, 1,
        ))
        connection.execute("INSERT INTO bili_bindings VALUES (?,?,?,?,?)", (456, "123", "first", "last", '["中文昵称"]'))
        connection.execute("INSERT INTO leave_records VALUES (?,?,?,?,?)", ("group", "123", "2026-10-19 01:02:03", "QQ昵称", 7))
        connection.execute("""INSERT INTO join_request_audits
        (created_at,group_id,qq,bili_name,bili_uid,result,reasons_json,condition_states_json,
         condition_reasons_json,stage_results_json,action_status,action_error,updated_at,request_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "before", "group", "123", "中文昵称", 456, "approved", '["真实原因"]', '{"follow":"true"}',
            '{"follow":"已关注"}', '["gate:true"]', "pending", "uncertain network", "before", "bot:group:add:flag",
        ))
        connection.commit()
        tables = ("metadata", "user_bindings", "bili_bindings", "join_request_audits", "leave_records")
        before = {name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")] for name in tables}
        connection.close()
        ConfigManager.initialize(SimpleNamespace(app_root=self.root))
        with legacy.database_connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            after = {name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")] for name in tables}
        self.assertEqual(before, after)
        self.assertEqual(JoinRequestRecordManager.get_pending_decision_count(), 1)
        self.assertEqual(JoinRequestRecordManager.get_pending_decisions()[0].request_key, "bot:group:add:flag")
        backups = list((self.path.parent / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(backup.execute("SELECT action_status FROM join_request_audits").fetchone()[0], "pending")
        if os.name == "posix":
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        from deploy.verify_migration import compare
        parity = compare(backups[0], self.path)
        self.assertTrue(parity['ok'])
        baseline = self.root / 'private-config-baseline.json'
        baseline.write_text(json.dumps({'bili_cookie': 'private-fixture-secret'}), encoding='utf-8')
        mismatch = compare(backups[0], self.path, baseline)
        self.assertFalse(mismatch['ok'])
        self.assertEqual(mismatch['config']['differing_paths'], ['config.bili_cookie'])
        self.assertNotIn('private-fixture-secret', json.dumps(mismatch))
        with legacy.database_connection() as connection, connection:
            connection.execute('UPDATE user_bindings SET latest_bili_name=?', ('private-changed-value',))
        drift = compare(backups[0], self.path)
        self.assertFalse(drift['ok'])
        self.assertNotIn('private-changed-value', json.dumps(drift))

    def test_pending_finalize_is_idempotent_and_binds_only_after_confirmation(self):
        legacy.initialize_database()
        audit = JoinRequestRecordManager.begin_pending_decision(
            group_id="10", qq="20", bili_uid=30, bili_name="name", result="approved", reasons=["pass"], request_key="unique",
        )
        duplicate = JoinRequestRecordManager.begin_pending_decision(
            group_id="10", qq="20", bili_uid=30, bili_name="name", result="approved", reasons=["pass"], request_key="unique",
        )
        self.assertFalse(duplicate.was_created)
        self.assertEqual(audit.id, duplicate.id)
        self.assertIsNone(JoinRequestRecordManager.get_bili_binding(30))
        self.assertTrue(JoinRequestRecordManager.finalize_pending_decision(audit.id, applied=True))
        self.assertTrue(JoinRequestRecordManager.finalize_pending_decision(audit.id, applied=True))
        self.assertEqual(JoinRequestRecordManager.get_bili_binding(30).owner_qq, "20")
        self.assertEqual(JoinRequestRecordManager.get_user_binding("20").approved_count, 1)

    def test_future_schema_is_rejected_before_changing_persistent_journal_mode(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 5")
            connection.execute("PRAGMA journal_mode = DELETE")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "5.*4"):
            legacy.initialize_database()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_runtime_state_updates_are_atomic_under_concurrent_writers(self):
        legacy.initialize_database()
        def increment(_):
            return RuntimeStateRepository.update("patrol", "count", lambda current: current + 1, default=0)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(increment, range(50)))
        self.assertEqual(RuntimeStateRepository.get("patrol", "count"), 50)
        self.assertEqual(RuntimeStateRepository.list_namespace("patrol"), {"count": 50})
        self.assertTrue(RuntimeStateRepository.delete("patrol", "count"))
        self.assertEqual(RuntimeStateRepository.get("patrol", "count", "missing"), "missing")

    def test_safe_admin_queries_are_bounded_and_filter_parameterized(self):
        legacy.initialize_database()
        for index in range(3):
            JoinRequestRecordManager.add_audit(group_id="group", qq="20", result="ignored", reasons=[str(index)])
        JoinRequestRecordManager.bind_identity("20", "name", 30, "group")
        LeaveRecordManager.add_leave_record("group", "20", "QQ name")
        self.assertEqual(len(AdminRecordRepository.audits(qq="20", limit=2)), 2)
        self.assertEqual(AdminRecordRepository.audits(qq="20' OR 1=1 --"), [])
        self.assertEqual(AdminRecordRepository.bindings(qq="20")[0]["bili_uid"], 30)
        self.assertEqual(AdminRecordRepository.leaves(group_id="group")[0]["qq"], "20")

    def test_legacy_json_and_leave_records_import_once(self):
        payload = {"user_bindings": {}, "bili_bindings": {}, "audits": [{"created_at": "old", "qq": "20", "group_id": "10", "result": "ignored"}]}
        legacy.LEGACY_JOIN_REQUEST_FILE.write_text(json.dumps(payload), encoding="utf-8")
        legacy.LEGACY_LEAVE_RECORDS_PATH.mkdir(parents=True)
        (legacy.LEGACY_LEAVE_RECORDS_PATH / "leave_records_10.json").write_text(json.dumps({"records": {"20": {"leave_time": "old", "count": 2}}}), encoding="utf-8")
        legacy.initialize_database()
        legacy.reset_database_state()
        legacy.initialize_database()
        self.assertEqual(len(JoinRequestRecordManager.get_user_audits("20")), 1)
        self.assertEqual(LeaveRecordManager.get_leave_record("10", "20").count, 2)


if __name__ == "__main__":
    unittest.main()
