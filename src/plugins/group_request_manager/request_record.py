"""SQLite-backed join request audit and QQ/Bilibili identity records."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .database import database_connection

logger = logging.getLogger("group_request_manager.request_record")


class JoinRequestAudit(BaseModel):
    """单次加群申请的审计记录。"""

    id: Optional[int] = None
    created_at: str
    group_id: str
    qq: str
    bili_name: str = ""
    bili_uid: Optional[int] = None
    result: str
    reasons: list[str] = Field(default_factory=list)
    follow_status: str = ""
    medal_status: str = ""
    leave_status: str = ""
    conflict_qq: str = ""
    condition_states: dict[str, str] = Field(default_factory=dict)
    condition_reasons: dict[str, str] = Field(default_factory=dict)
    stage_results: list[str] = Field(default_factory=list)
    action_status: str = "not_required"
    action_error: str = ""
    updated_at: str = ""
    request_key: str = ""
    was_created: bool = False


class UserAuditSummary(BaseModel):
    """用于管理员查询的轻量审计统计。"""

    attempt_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    ignored_count: int = 0
    pending_count: int = 0
    groups: list[str] = Field(default_factory=list)


class UserBinding(BaseModel):
    """单个 QQ 的历史绑定信息。"""

    qq: str
    latest_bili_name: str = ""
    latest_bili_uid: Optional[int] = None
    first_seen: str = ""
    last_seen: str = ""
    groups: list[str] = Field(default_factory=list)
    used_bili_uids: list[int] = Field(default_factory=list)
    used_bili_names: list[str] = Field(default_factory=list)
    attempt_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    ignored_count: int = 0


class BiliBinding(BaseModel):
    """B站 UID 的归属记录。"""

    bili_uid: int
    owner_qq: str
    first_seen: str
    last_seen: str
    bili_names: list[str] = Field(default_factory=list)


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(raw: str, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("SQLite 中存在无法解析的 JSON 字段，将使用默认值")
        return default


def _user_binding_from_row(row: sqlite3.Row) -> UserBinding:
    return UserBinding(
        qq=row["qq"],
        latest_bili_name=row["latest_bili_name"],
        latest_bili_uid=row["latest_bili_uid"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        groups=_json_load(row["groups_json"], []),
        used_bili_uids=_json_load(row["used_bili_uids_json"], []),
        used_bili_names=_json_load(row["used_bili_names_json"], []),
        attempt_count=row["attempt_count"],
        approved_count=row["approved_count"],
        rejected_count=row["rejected_count"],
        ignored_count=row["ignored_count"],
    )


def _bili_binding_from_row(row: sqlite3.Row) -> BiliBinding:
    return BiliBinding(
        bili_uid=row["bili_uid"],
        owner_qq=row["owner_qq"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        bili_names=_json_load(row["bili_names_json"], []),
    )


def _audit_from_row(row: sqlite3.Row) -> JoinRequestAudit:
    return JoinRequestAudit(
        id=row["id"],
        created_at=row["created_at"],
        group_id=row["group_id"],
        qq=row["qq"],
        bili_name=row["bili_name"],
        bili_uid=row["bili_uid"],
        result=row["result"],
        reasons=_json_load(row["reasons_json"], []),
        follow_status=row["follow_status"],
        medal_status=row["medal_status"],
        leave_status=row["leave_status"],
        conflict_qq=row["conflict_qq"],
        condition_states=_json_load(row["condition_states_json"], {}),
        condition_reasons=_json_load(row["condition_reasons_json"], {}),
        stage_results=_json_load(row["stage_results_json"], []),
        action_status=row["action_status"],
        action_error=row["action_error"],
        updated_at=row["updated_at"],
        request_key=row["request_key"] or "",
    )


class JoinRequestRecordManager:
    """加群申请记录与绑定关系管理器。"""

    _save_failure_count: int = 0
    _lock = threading.RLock()

    @classmethod
    def _now_str(cls) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _save_user_binding(
        cls, connection: sqlite3.Connection, binding: UserBinding
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO user_bindings (
                qq, latest_bili_name, latest_bili_uid, first_seen, last_seen,
                groups_json, used_bili_uids_json, used_bili_names_json,
                attempt_count, approved_count, rejected_count, ignored_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.qq,
                binding.latest_bili_name,
                binding.latest_bili_uid,
                binding.first_seen,
                binding.last_seen,
                _json_dump(binding.groups),
                _json_dump(binding.used_bili_uids),
                _json_dump(binding.used_bili_names),
                binding.attempt_count,
                binding.approved_count,
                binding.rejected_count,
                binding.ignored_count,
            ),
        )

    @classmethod
    def _touch_user_binding(
        cls,
        connection: sqlite3.Connection,
        *,
        qq: str,
        group_id: str,
        bili_name: str = "",
        bili_uid: Optional[int] = None,
        result: Optional[str] = None,
        count_attempt: bool = True,
    ) -> UserBinding:
        row = connection.execute(
            "SELECT * FROM user_bindings WHERE qq = ?", (qq,)
        ).fetchone()
        now = cls._now_str()
        binding = (
            _user_binding_from_row(row)
            if row
            else UserBinding(qq=qq, first_seen=now, last_seen=now)
        )

        binding.last_seen = now
        if count_attempt:
            binding.attempt_count += 1
        if group_id and group_id not in binding.groups:
            binding.groups.append(group_id)
        if bili_name:
            binding.latest_bili_name = bili_name
            if bili_name not in binding.used_bili_names:
                binding.used_bili_names.append(bili_name)
        if bili_uid is not None:
            binding.latest_bili_uid = bili_uid
            if bili_uid not in binding.used_bili_uids:
                binding.used_bili_uids.append(bili_uid)

        if result == "approved":
            binding.approved_count += 1
        elif result == "rejected":
            binding.rejected_count += 1
        elif result == "ignored":
            binding.ignored_count += 1

        cls._save_user_binding(connection, binding)
        return binding

    @classmethod
    def _record_bili_binding_if_owner(
        cls,
        connection: sqlite3.Connection,
        qq: str,
        bili_name: str,
        bili_uid: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM bili_bindings WHERE bili_uid = ?", (bili_uid,)
        ).fetchone()
        now = cls._now_str()
        if row:
            binding = _bili_binding_from_row(row)
            if binding.owner_qq != qq:
                return
            binding.last_seen = now
            if bili_name and bili_name not in binding.bili_names:
                binding.bili_names.append(bili_name)
        else:
            binding = BiliBinding(
                bili_uid=bili_uid,
                owner_qq=qq,
                first_seen=now,
                last_seen=now,
                bili_names=[bili_name] if bili_name else [],
            )

        connection.execute(
            """
            INSERT OR REPLACE INTO bili_bindings (
                bili_uid, owner_qq, first_seen, last_seen, bili_names_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                binding.bili_uid,
                binding.owner_qq,
                binding.first_seen,
                binding.last_seen,
                _json_dump(binding.bili_names),
            ),
        )

    @classmethod
    def get_bili_binding(cls, bili_uid: int) -> Optional[BiliBinding]:
        with cls._lock, database_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bili_bindings WHERE bili_uid = ?", (bili_uid,)
            ).fetchone()
            return _bili_binding_from_row(row) if row else None

    @classmethod
    def get_conflict_qq(cls, qq: str, bili_uid: int) -> Optional[str]:
        binding = cls.get_bili_binding(bili_uid)
        if not binding or binding.owner_qq == qq:
            return None
        return binding.owner_qq

    @classmethod
    def get_owned_bili_uids(cls, qq: str) -> list[int]:
        with cls._lock, database_connection() as connection:
            rows = connection.execute(
                "SELECT bili_uid FROM bili_bindings WHERE owner_qq = ? ORDER BY bili_uid",
                (qq,),
            ).fetchall()
            return [row["bili_uid"] for row in rows]

    @classmethod
    def clear_identity_binding(cls, qq: str) -> list[int]:
        with cls._lock, database_connection() as connection, connection:
            rows = connection.execute(
                "SELECT bili_uid FROM bili_bindings WHERE owner_qq = ? ORDER BY bili_uid",
                (qq,),
            ).fetchall()
            removed_uids = [row["bili_uid"] for row in rows]
            connection.execute("DELETE FROM bili_bindings WHERE owner_qq = ?", (qq,))
            connection.execute(
                """
                UPDATE user_bindings
                SET latest_bili_name = '', latest_bili_uid = NULL,
                    used_bili_names_json = '[]', used_bili_uids_json = '[]'
                WHERE qq = ?
                """,
                (qq,),
            )
            return removed_uids

    @classmethod
    def bind_identity(cls, qq: str, bili_name: str, bili_uid: int, group_id: str) -> None:
        with cls._lock, database_connection() as connection, connection:
            cls._touch_user_binding(
                connection,
                qq=qq,
                group_id=group_id,
                bili_name=bili_name,
                bili_uid=bili_uid,
                result=None,
                count_attempt=False,
            )
            cls._record_bili_binding_if_owner(connection, qq, bili_name, bili_uid)

    @classmethod
    def _new_audit(
        cls,
        *,
        group_id: str,
        qq: str,
        result: str,
        reasons: list[str],
        bili_name: str = "",
        bili_uid: Optional[int] = None,
        follow_status: str = "",
        medal_status: str = "",
        leave_status: str = "",
        conflict_qq: Optional[str] = None,
        condition_states: Optional[dict[str, str]] = None,
        condition_reasons: Optional[dict[str, str]] = None,
        stage_results: Optional[list[str]] = None,
        action_status: str = "not_required",
        request_key: str = "",
    ) -> JoinRequestAudit:
        now = cls._now_str()
        return JoinRequestAudit(
            created_at=now,
            group_id=group_id,
            qq=qq,
            bili_name=bili_name,
            bili_uid=bili_uid,
            result=result,
            reasons=reasons,
            follow_status=follow_status,
            medal_status=medal_status,
            leave_status=leave_status,
            conflict_qq=conflict_qq or "",
            condition_states=condition_states or {},
            condition_reasons=condition_reasons or {},
            stage_results=stage_results or [],
            action_status=action_status,
            updated_at=now,
            request_key=request_key,
            was_created=True,
        )

    @classmethod
    def _insert_audit(
        cls,
        connection: sqlite3.Connection,
        audit: JoinRequestAudit,
        *,
        ignore_request_conflict: bool = False,
    ) -> Optional[int]:
        conflict_clause = (
            " ON CONFLICT(request_key) DO NOTHING" if ignore_request_conflict else ""
        )
        cursor = connection.execute(
            f"""
            INSERT INTO join_request_audits (
                created_at, group_id, qq, bili_name, bili_uid, result,
                reasons_json, follow_status, medal_status, leave_status,
                conflict_qq, condition_states_json, condition_reasons_json,
                stage_results_json, action_status, action_error, updated_at,
                request_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            {conflict_clause}
            """,
            (
                audit.created_at,
                audit.group_id,
                audit.qq,
                audit.bili_name,
                audit.bili_uid,
                audit.result,
                _json_dump(audit.reasons),
                audit.follow_status,
                audit.medal_status,
                audit.leave_status,
                audit.conflict_qq,
                _json_dump(audit.condition_states),
                _json_dump(audit.condition_reasons),
                _json_dump(audit.stage_results),
                audit.action_status,
                audit.action_error,
                audit.updated_at,
                audit.request_key or None,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return int(cursor.lastrowid)

    @classmethod
    def _apply_audit_to_bindings(
        cls, connection: sqlite3.Connection, audit: JoinRequestAudit
    ) -> None:
        cls._touch_user_binding(
            connection,
            qq=audit.qq,
            group_id=audit.group_id,
            bili_name=audit.bili_name,
            bili_uid=audit.bili_uid,
            result=audit.result,
        )
        if audit.bili_uid is None or audit.conflict_qq:
            return
        existing_row = connection.execute(
            "SELECT owner_qq FROM bili_bindings WHERE bili_uid = ?",
            (audit.bili_uid,),
        ).fetchone()
        should_update_binding = audit.result == "approved" or (
            existing_row is not None and existing_row["owner_qq"] == audit.qq
        )
        if should_update_binding:
            cls._record_bili_binding_if_owner(
                connection, audit.qq, audit.bili_name, audit.bili_uid
            )

    @classmethod
    def _persist_new_audit(
        cls,
        audit: JoinRequestAudit,
        *,
        update_bindings: bool,
        deduplicate_request: bool = False,
    ) -> Optional[JoinRequestAudit]:
        with cls._lock:
            for attempt in range(3):
                try:
                    with database_connection() as connection, connection:
                        audit.id = cls._insert_audit(
                            connection,
                            audit,
                            ignore_request_conflict=deduplicate_request,
                        )
                        if audit.id is None:
                            row = connection.execute(
                                "SELECT * FROM join_request_audits WHERE request_key = ?",
                                (audit.request_key,),
                            ).fetchone()
                            return _audit_from_row(row) if row else None
                        if update_bindings:
                            cls._apply_audit_to_bindings(connection, audit)
                    cls._save_failure_count = 0
                    return audit
                except sqlite3.OperationalError as exc:
                    cls._save_failure_count += 1
                    if "locked" in str(exc).lower() and attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    logger.exception(
                        "保存申请记录到 SQLite 失败（尝试 %s/3）", attempt + 1
                    )
                    break
                except Exception:
                    cls._save_failure_count += 1
                    logger.exception(
                        "保存申请记录到 SQLite 时发生异常：QQ=%s, group_id=%s",
                        audit.qq,
                        audit.group_id,
                    )
                    break

        logger.critical(
            "申请记录未写入 SQLite：QQ=%s, result=%s, reasons=%s",
            audit.qq,
            audit.result,
            audit.reasons,
        )
        return None

    @classmethod
    def add_audit(
        cls,
        *,
        group_id: str,
        qq: str,
        result: str,
        reasons: list[str],
        bili_name: str = "",
        bili_uid: Optional[int] = None,
        follow_status: str = "",
        medal_status: str = "",
        leave_status: str = "",
        conflict_qq: Optional[str] = None,
        condition_states: Optional[dict[str, str]] = None,
        condition_reasons: Optional[dict[str, str]] = None,
        stage_results: Optional[list[str]] = None,
    ) -> Optional[JoinRequestAudit]:
        """在一个事务中记录申请、统计信息和可用的身份归属。"""
        audit = cls._new_audit(
            group_id=group_id,
            qq=qq,
            result=result,
            reasons=reasons,
            bili_name=bili_name,
            bili_uid=bili_uid,
            follow_status=follow_status,
            medal_status=medal_status,
            leave_status=leave_status,
            conflict_qq=conflict_qq,
            condition_states=condition_states,
            condition_reasons=condition_reasons,
            stage_results=stage_results,
        )
        return cls._persist_new_audit(audit, update_bindings=True)

    @classmethod
    def begin_pending_decision(cls, **audit_kwargs) -> Optional[JoinRequestAudit]:
        """Persist the intended external action before calling OneBot."""
        audit = cls._new_audit(action_status="pending", **audit_kwargs)
        return cls._persist_new_audit(
            audit,
            update_bindings=False,
            deduplicate_request=bool(audit.request_key),
        )

    @classmethod
    def finalize_pending_decision(
        cls,
        audit_id: int,
        *,
        applied: bool,
        failure_reason: str = "",
    ) -> bool:
        """Finalize a pending external action and update user statistics once."""
        with cls._lock:
            for attempt in range(3):
                try:
                    with database_connection() as connection, connection:
                        row = connection.execute(
                            "SELECT * FROM join_request_audits WHERE id = ?",
                            (audit_id,),
                        ).fetchone()
                        if row is None:
                            return False
                        audit = _audit_from_row(row)
                        if audit.action_status != "pending":
                            expected_status = "applied" if applied else "failed"
                            return audit.action_status == expected_status

                        audit.action_status = "applied" if applied else "failed"
                        audit.action_error = "" if applied else failure_reason
                        audit.updated_at = cls._now_str()
                        if not applied:
                            audit.result = "ignored"
                            audit.reasons = [failure_reason or "调用加群审批接口失败"]
                        cursor = connection.execute(
                            """
                            UPDATE join_request_audits
                            SET result = ?, reasons_json = ?, action_status = ?,
                                action_error = ?, updated_at = ?
                            WHERE id = ? AND action_status = 'pending'
                            """,
                            (
                                audit.result,
                                _json_dump(audit.reasons),
                                audit.action_status,
                                audit.action_error,
                                audit.updated_at,
                                audit_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            return False
                        cls._apply_audit_to_bindings(connection, audit)
                    cls._save_failure_count = 0
                    return True
                except sqlite3.OperationalError as exc:
                    cls._save_failure_count += 1
                    if "locked" in str(exc).lower() and attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    logger.exception("更新待执行审计失败：id=%s", audit_id)
                    break
                except Exception:
                    cls._save_failure_count += 1
                    logger.exception("更新待执行审计时发生异常：id=%s", audit_id)
                    break
        return False

    @classmethod
    def get_user_summary(cls, qq: str) -> UserAuditSummary:
        with cls._lock, database_connection() as connection:
            return cls._get_user_summary(connection, qq)

    @classmethod
    def _get_user_summary(
        cls, connection: sqlite3.Connection, qq: str
    ) -> UserAuditSummary:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS attempt_count,
                SUM(CASE WHEN result = 'approved' AND action_status != 'pending' THEN 1 ELSE 0 END) AS approved_count,
                SUM(CASE WHEN result = 'rejected' AND action_status != 'pending' THEN 1 ELSE 0 END) AS rejected_count,
                SUM(CASE WHEN result = 'ignored' AND action_status != 'pending' THEN 1 ELSE 0 END) AS ignored_count,
                SUM(CASE WHEN action_status = 'pending' THEN 1 ELSE 0 END) AS pending_count
            FROM join_request_audits WHERE qq = ?
            """,
            (qq,),
        ).fetchone()
        group_rows = connection.execute(
            """
            SELECT group_id, MIN(id) AS first_id
            FROM join_request_audits
            WHERE qq = ? AND group_id != ''
            GROUP BY group_id ORDER BY first_id
            """,
            (qq,),
        ).fetchall()
        return UserAuditSummary(
            attempt_count=int(row["attempt_count"] or 0),
            approved_count=int(row["approved_count"] or 0),
            rejected_count=int(row["rejected_count"] or 0),
            ignored_count=int(row["ignored_count"] or 0),
            pending_count=int(row["pending_count"] or 0),
            groups=[str(group_row["group_id"]) for group_row in group_rows],
        )

    @classmethod
    def get_user_report(
        cls, qq: str, limit: int = 5
    ) -> tuple[Optional[UserBinding], UserAuditSummary, list[JoinRequestAudit]]:
        """Load all data needed by /检查qq using one worker and connection."""
        with cls._lock, database_connection() as connection:
            binding_row = connection.execute(
                "SELECT * FROM user_bindings WHERE qq = ?", (qq,)
            ).fetchone()
            summary = cls._get_user_summary(connection, qq)
            audit_rows = connection.execute(
                """
                SELECT * FROM join_request_audits
                WHERE qq = ? ORDER BY id DESC LIMIT ?
                """,
                (qq, max(0, limit)),
            ).fetchall()
        return (
            _user_binding_from_row(binding_row) if binding_row else None,
            summary,
            [_audit_from_row(audit_row) for audit_row in audit_rows],
        )

    @classmethod
    def get_pending_decision_count(cls) -> int:
        with cls._lock, database_connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM join_request_audits WHERE action_status = 'pending'"
            ).fetchone()
            return int(row[0])

    @classmethod
    def get_pending_decisions(cls, limit: int = 20) -> list[JoinRequestAudit]:
        if limit <= 0:
            return []
        with cls._lock, database_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM join_request_audits
                WHERE action_status = 'pending'
                ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_audit_from_row(row) for row in rows]

    @classmethod
    def get_user_binding(cls, qq: str) -> Optional[UserBinding]:
        with cls._lock, database_connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_bindings WHERE qq = ?", (qq,)
            ).fetchone()
            return _user_binding_from_row(row) if row else None

    @classmethod
    def get_all_user_audits(cls, qq: str) -> list[JoinRequestAudit]:
        with cls._lock, database_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM join_request_audits WHERE qq = ? ORDER BY id", (qq,)
            ).fetchall()
            return [_audit_from_row(row) for row in rows]

    @classmethod
    def get_user_audits(cls, qq: str, limit: int = 10) -> list[JoinRequestAudit]:
        if limit <= 0:
            return []
        with cls._lock, database_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM join_request_audits
                WHERE qq = ? ORDER BY id DESC LIMIT ?
                """,
                (qq, limit),
            ).fetchall()
            return [_audit_from_row(row) for row in rows]
