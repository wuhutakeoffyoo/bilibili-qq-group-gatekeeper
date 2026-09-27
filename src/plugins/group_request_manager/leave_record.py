"""SQLite-backed leave record management."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from .database import database_connection

logger = logging.getLogger("group_request_manager.leave_record")


class LeaveRecord(BaseModel):
    """单条退群记录。"""

    qq: str
    leave_time: str
    nickname: str
    count: int = 1


class LeaveRecordManager:
    """通过 SQLite 保存和查询退群记录。"""

    _lock = threading.RLock()

    @classmethod
    def format_leave_time(cls, leave_time: str) -> str:
        """将存量 ISO 时间统一转成更适合聊天展示的格式。"""
        if not leave_time:
            return ""
        normalized = leave_time.strip()
        for candidate in (normalized, normalized.replace("T", " ")):
            try:
                dt = datetime.fromisoformat(candidate)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        return normalized.replace("T", " ")

    @classmethod
    def _record_from_row(cls, row) -> LeaveRecord:
        return LeaveRecord(
            qq=row["qq"],
            leave_time=row["leave_time"],
            nickname=row["nickname"],
            count=row["count"],
        )

    @classmethod
    def add_leave_record(cls, group_id: str, qq: str, nickname: str) -> LeaveRecord:
        """新增退群记录；同一用户再次退群时累加次数。"""
        leave_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with cls._lock, database_connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO leave_records (group_id, qq, leave_time, nickname, count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(group_id, qq) DO UPDATE SET
                    leave_time = excluded.leave_time,
                    nickname = excluded.nickname,
                    count = leave_records.count + 1
                """,
                (group_id, qq, leave_time, nickname),
            )
            row = connection.execute(
                "SELECT * FROM leave_records WHERE group_id = ? AND qq = ?",
                (group_id, qq),
            ).fetchone()
            assert row is not None
            return cls._record_from_row(row)

    @classmethod
    def get_leave_record(cls, group_id: str, qq: str) -> Optional[LeaveRecord]:
        with cls._lock, database_connection() as connection:
            row = connection.execute(
                "SELECT * FROM leave_records WHERE group_id = ? AND qq = ?",
                (group_id, qq),
            ).fetchone()
            return cls._record_from_row(row) if row else None

    @classmethod
    def get_all_records(cls, group_id: str) -> list[LeaveRecord]:
        with cls._lock, database_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM leave_records WHERE group_id = ? ORDER BY qq",
                (group_id,),
            ).fetchall()
            return [cls._record_from_row(row) for row in rows]

    @classmethod
    def remove_leave_record(cls, group_id: str, qq: str) -> bool:
        with cls._lock, database_connection() as connection, connection:
            cursor = connection.execute(
                "DELETE FROM leave_records WHERE group_id = ? AND qq = ?",
                (group_id, qq),
            )
            return cursor.rowcount > 0

    @classmethod
    def remove_leave_record_from_all_groups(cls, qq: str) -> list[str]:
        with cls._lock, database_connection() as connection, connection:
            rows = connection.execute(
                "SELECT group_id FROM leave_records WHERE qq = ? ORDER BY group_id",
                (qq,),
            ).fetchall()
            affected_group_ids = [row["group_id"] for row in rows]
            connection.execute("DELETE FROM leave_records WHERE qq = ?", (qq,))
            return affected_group_ids
