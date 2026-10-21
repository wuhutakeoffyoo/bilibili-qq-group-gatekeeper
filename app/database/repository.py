"""Small transactional repositories shared by framework-free services.

Revision payloads and source manifests contain credentials. Only explicit internal
access returns them; administrator snapshots are redacted in app.config.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .legacy import database_connection


class ConfigVersionConflict(ValueError):
    """The submitted configuration was edited from an obsolete revision."""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ConfigRepository:
    @staticmethod
    def current() -> dict[str, Any] | None:
        with database_connection() as connection:
            row = connection.execute(
                "SELECT r.* FROM config_head h JOIN config_revisions r ON r.version = h.version WHERE h.singleton = 1"
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def get(version: int) -> dict[str, Any] | None:
        with database_connection() as connection:
            row = connection.execute("SELECT * FROM config_revisions WHERE version = ?", (int(version),)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def append(
        config: dict[str, Any], *, expected_version: int, actor: str = "",
        manifest: dict[str, Any] | None = None, restored_from: int | None = None,
    ) -> int:
        with database_connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute("SELECT version FROM config_head WHERE singleton = 1").fetchone()
            current = int(head[0]) if head else 0
            if current != int(expected_version):
                raise ConfigVersionConflict(f"配置版本冲突：当前版本 {current}，提交版本 {expected_version}；请刷新后重试")
            if manifest is None and current:
                previous = connection.execute(
                    "SELECT manifest_json FROM config_revisions WHERE version = ?", (current,)
                ).fetchone()
                manifest_json = previous[0]
            else:
                manifest_json = _dump(manifest or {})
            cursor = connection.execute(
                """INSERT INTO config_revisions
                (created_at, actor, config_json, manifest_json, parent_version, restored_from)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (time.time(), str(actor), _dump(config), manifest_json, current or None, restored_from),
            )
            version = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO config_head(singleton,version) VALUES (1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version", (version,),
            )
            return version

    @staticmethod
    def versions(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT version,created_at,actor,parent_version,restored_from FROM config_revisions ORDER BY version DESC LIMIT ? OFFSET ?",
                (min(500, max(1, int(limit))), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows]


class RuntimeStateRepository:
    @staticmethod
    def get(namespace: str, key: str, default: Any = None) -> Any:
        with database_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE namespace=? AND key=?", (str(namespace), str(key))
            ).fetchone()
            return json.loads(row[0]) if row else default

    @staticmethod
    def set(namespace: str, key: str, value: Any) -> None:
        RuntimeStateRepository.update(namespace, key, lambda _previous: value)

    @staticmethod
    def update(namespace: str, key: str, mutator: Callable[[Any], Any], default: Any = None) -> Any:
        """Read and replace a value in one SQLite write transaction.

        Mutators must not acquire ConfigManager's lock or open another write
        transaction: config writers acquire their lock before SQLite, so the
        opposite ordering can deadlock. Capture immutable inputs beforehand.
        """
        with database_connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE namespace=? AND key=?", (str(namespace), str(key))
            ).fetchone()
            value = mutator(json.loads(row[0]) if row else default)
            connection.execute(
                """INSERT INTO runtime_state(namespace,key,value_json,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(namespace,key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (str(namespace), str(key), _dump(value), time.time()),
            )
            return value

    @staticmethod
    def delete(namespace: str, key: str) -> bool:
        with database_connection() as connection, connection:
            cursor = connection.execute("DELETE FROM runtime_state WHERE namespace=? AND key=?", (str(namespace), str(key)))
            return cursor.rowcount > 0

    @staticmethod
    def list_namespace(namespace: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT key,value_json FROM runtime_state WHERE namespace=? ORDER BY key LIMIT ? OFFSET ?",
                (str(namespace), min(1000, max(1, int(limit))), max(0, int(offset))),
            ).fetchall()
            return {str(row[0]): json.loads(row[1]) for row in rows}


get_runtime_state = RuntimeStateRepository.get
set_runtime_state = RuntimeStateRepository.set


class AdminRecordRepository:
    """Bounded, parameterized queries. Authentication belongs to the HTTP layer."""

    @staticmethod
    def audits(*, qq: str = "", group_id: str = "", bili_uid: int | None = None,
               action_status: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        from .records import _audit_from_row
        clauses, params = [], []
        for column, value in (("qq", qq), ("group_id", group_id), ("bili_uid", bili_uid), ("action_status", action_status)):
            if value not in ("", None):
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM join_request_audits" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, min(500, max(1, int(limit))), max(0, int(offset))),
            ).fetchall()
            return [_audit_from_row(row).model_dump() for row in rows]

    @staticmethod
    def bindings(*, qq: str = "", bili_uid: int | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        from .records import _bili_binding_from_row
        clauses, params = [], []
        if qq:
            clauses.append("owner_qq = ?")
            params.append(qq)
        if bili_uid is not None:
            clauses.append("bili_uid = ?")
            params.append(int(bili_uid))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bili_bindings" + where + " ORDER BY bili_uid LIMIT ? OFFSET ?",
                (*params, min(500, max(1, int(limit))), max(0, int(offset))),
            ).fetchall()
            return [_bili_binding_from_row(row).model_dump() for row in rows]

    @staticmethod
    def leaves(*, qq: str = "", group_id: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (("qq", qq), ("group_id", group_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM leave_records" + where + " ORDER BY leave_time DESC,group_id,qq LIMIT ? OFFSET ?",
                (*params, min(500, max(1, int(limit))), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows]
