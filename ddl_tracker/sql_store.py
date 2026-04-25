"""SQLite 存储层，集中管理所有 SQL 操作。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .utils import safe_int


class DDLStore:
    """负责插件所有数据库读写。"""

    def __init__(self, db_path: Path):
        """保存 SQLite 文件路径。"""
        self.db_path = db_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """创建一个带基础 PRAGMA 的数据库连接。"""
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def use_connection(self, conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        """复用外部连接，或按需创建新连接。"""
        if conn is not None:
            yield conn
            return
        with self.connection() as managed:
            yield managed

    def initialize(self):
        """初始化插件所需的全部数据表和索引。"""
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    unified_msg_origin TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_extract_at INTEGER NOT NULL DEFAULT 0,
                    last_message_row_id INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    unified_msg_origin TEXT NOT NULL,
                    sender_id TEXT DEFAULT '',
                    sender_name TEXT DEFAULT '',
                    sender_role TEXT DEFAULT '',
                    message_text TEXT NOT NULL,
                    message_ts INTEGER NOT NULL,
                    is_command INTEGER NOT NULL DEFAULT 0,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_group_id_id
                ON messages(group_id, id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_group_ts
                ON messages(group_id, message_ts)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    remind_type TEXT NOT NULL,
                    offset_minutes INTEGER NOT NULL DEFAULT 0,
                    days_before INTEGER NOT NULL DEFAULT 0,
                    fixed_hour INTEGER NOT NULL DEFAULT -1,
                    fixed_minute INTEGER NOT NULL DEFAULT -1,
                    source_text TEXT DEFAULT '',
                    source_message_ids TEXT DEFAULT '[]',
                    created_by_sender_id TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(group_id, category)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminder_rules_group_category
                ON reminder_rules(group_id, category)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '其他',
                    deadline_text TEXT DEFAULT '',
                    deadline_ts INTEGER NOT NULL,
                    remind_at_ts INTEGER NOT NULL,
                    remind_rule_type TEXT NOT NULL DEFAULT 'offset',
                    remind_rule_value TEXT DEFAULT '',
                    source_message_ids TEXT DEFAULT '[]',
                    source_text TEXT DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reminded INTEGER NOT NULL DEFAULT 0,
                    reminded_at INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    extracted_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(group_id, fingerprint)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_deadlines_group_status_deadline
                ON deadlines(group_id, status, deadline_ts)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_deadlines_group_status_remind
                ON deadlines(group_id, status, reminded, remind_at_ts)
                """
            )
            conn.commit()

    def enable_group(self, group_id: str, unified_msg_origin: str, now_ts: int):
        """启用某个群的 DDL 跟踪。"""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO groups (
                    group_id,
                    unified_msg_origin,
                    enabled,
                    last_extract_at,
                    last_message_row_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 1, ?, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    unified_msg_origin=excluded.unified_msg_origin,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (group_id, unified_msg_origin, now_ts, now_ts, now_ts),
            )
            conn.commit()

    def disable_group(self, group_id: str, now_ts: int):
        """关闭某个群的 DDL 跟踪。"""
        with self.connection() as conn:
            conn.execute(
                "UPDATE groups SET enabled=0, updated_at=? WHERE group_id=?",
                (now_ts, group_id),
            )
            conn.commit()

    def get_group(self, group_id: str) -> dict | None:
        """读取单个群的状态信息。"""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT group_id, unified_msg_origin, enabled, last_extract_at, last_message_row_id,
                       created_at, updated_at
                FROM groups
                WHERE group_id=?
                """,
                (group_id,),
            ).fetchone()
        return self._row_to_group(row)

    def group_enabled(self, group_id: str) -> bool:
        """判断某个群是否已启用插件。"""
        group = self.get_group(group_id)
        return bool(group and group["enabled"])

    def count_messages(self, group_id: str) -> int:
        """统计某个群已记录的消息数。"""
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages WHERE group_id=?", (group_id,)).fetchone()[0])

    def count_pending_deadlines(self, group_id: str) -> int:
        """统计某个群未完成的 DDL 数量。"""
        with self.connection() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM deadlines WHERE group_id=? AND status='pending'",
                    (group_id,),
                ).fetchone()[0]
            )

    def count_rules(self, group_id: str) -> int:
        """统计某个群的提醒规则数量。"""
        with self.connection() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM reminder_rules WHERE group_id=?",
                    (group_id,),
                ).fetchone()[0]
            )

    def get_next_pending_deadline(self, group_id: str) -> dict | None:
        """读取某个群最近的未完成 DDL。"""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, description, category, deadline_ts, remind_at_ts, reminded, confidence
                FROM deadlines
                WHERE group_id=? AND status='pending'
                ORDER BY deadline_ts ASC
                LIMIT 1
                """,
                (group_id,),
            ).fetchone()
        return self._row_to_deadline(row)

    def list_group_rules(self, group_id: str) -> list[dict]:
        """列出某个群的全部提醒规则。"""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT category, remind_type, offset_minutes, days_before, fixed_hour, fixed_minute,
                       source_text, source_message_ids, created_by_sender_id
                FROM reminder_rules
                WHERE group_id=?
                ORDER BY category ASC
                """,
                (group_id,),
            ).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def insert_message(self, message: dict):
        """插入一条群消息记录。"""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    group_id,
                    unified_msg_origin,
                    sender_id,
                    sender_name,
                    sender_role,
                    message_text,
                    message_ts,
                    is_command,
                    is_bot,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message["group_id"],
                    message["unified_msg_origin"],
                    message["sender_id"],
                    message["sender_name"],
                    message["sender_role"],
                    message["message_text"],
                    message["message_ts"],
                    1 if message["is_command"] else 0,
                    1 if message["is_bot"] else 0,
                    message["created_at"],
                ),
            )
            conn.commit()

    def list_enabled_groups(self) -> list[dict]:
        """列出所有已启用插件的群。"""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT group_id, unified_msg_origin, enabled, last_extract_at, last_message_row_id,
                       created_at, updated_at
                FROM groups
                WHERE enabled=1
                """
            ).fetchall()
        return [self._row_to_group(row) for row in rows]

    def touch_group_extract(
        self,
        group_id: str,
        now_ts: int,
        last_message_row_id: int | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        """更新某个群的抽取进度。"""
        with self.use_connection(conn) as active:
            if last_message_row_id is None:
                active.execute(
                    "UPDATE groups SET last_extract_at=?, updated_at=? WHERE group_id=?",
                    (now_ts, now_ts, group_id),
                )
            else:
                active.execute(
                    """
                    UPDATE groups
                    SET last_extract_at=?, last_message_row_id=?, updated_at=?
                    WHERE group_id=?
                    """,
                    (now_ts, last_message_row_id, now_ts, group_id),
                )
            if conn is None:
                active.commit()

    def fetch_extract_batch(self, group_id: str, last_message_row_id: int, limit: int) -> list[dict]:
        """读取某个群尚未处理的一批普通文本消息。"""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, group_id, unified_msg_origin, sender_id, sender_name, sender_role,
                       message_text, message_ts, is_command, is_bot, created_at
                FROM messages
                WHERE group_id=?
                  AND id>?
                  AND is_bot=0
                  AND is_command=0
                ORDER BY id ASC
                LIMIT ?
                """,
                (group_id, last_message_row_id, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def mark_overdue_deadlines(self, now_ts: int):
        """把已经过期的未完成 DDL 标记为 expired。"""
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE deadlines
                SET status='expired', updated_at=?
                WHERE status='pending' AND deadline_ts<=?
                """,
                (now_ts, now_ts),
            )
            conn.commit()

    def list_due_reminders(self, now_ts: int) -> list[dict]:
        """列出当前已经到达提醒时间的 DDL。"""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    d.id,
                    d.group_id,
                    d.description,
                    d.category,
                    d.deadline_ts,
                    d.remind_at_ts,
                    g.unified_msg_origin
                FROM deadlines d
                JOIN groups g ON g.group_id = d.group_id
                WHERE g.enabled=1
                  AND d.status='pending'
                  AND d.reminded=0
                  AND d.remind_at_ts<=?
                  AND d.deadline_ts>?
                ORDER BY d.remind_at_ts ASC, d.deadline_ts ASC
                """,
                (now_ts, now_ts),
            ).fetchall()
        return [self._row_to_reminder_dispatch(row) for row in rows]

    def mark_deadline_reminded(self, deadline_id: int, now_ts: int):
        """把某条 DDL 标记为已提醒。"""
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE deadlines
                SET reminded=1, reminded_at=?, updated_at=?
                WHERE id=?
                """,
                (now_ts, now_ts, deadline_id),
            )
            conn.commit()

    def upsert_reminder_rule(
        self,
        group_id: str,
        rule: dict,
        now_ts: int,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """插入或更新某个群的分类提醒规则。"""
        with self.use_connection(conn) as active:
            previous = active.execute(
                """
                SELECT remind_type, offset_minutes, days_before, fixed_hour, fixed_minute
                FROM reminder_rules
                WHERE group_id=? AND category=?
                """,
                (group_id, rule["category"]),
            ).fetchone()

            active.execute(
                """
                INSERT INTO reminder_rules (
                    group_id,
                    category,
                    remind_type,
                    offset_minutes,
                    days_before,
                    fixed_hour,
                    fixed_minute,
                    source_text,
                    source_message_ids,
                    created_by_sender_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, category) DO UPDATE SET
                    remind_type=excluded.remind_type,
                    offset_minutes=excluded.offset_minutes,
                    days_before=excluded.days_before,
                    fixed_hour=excluded.fixed_hour,
                    fixed_minute=excluded.fixed_minute,
                    source_text=excluded.source_text,
                    source_message_ids=excluded.source_message_ids,
                    created_by_sender_id=excluded.created_by_sender_id,
                    updated_at=excluded.updated_at
                """,
                (
                    group_id,
                    rule["category"],
                    rule["remind_type"],
                    rule.get("offset_minutes", 0),
                    rule.get("days_before", 0),
                    rule.get("fixed_hour", -1),
                    rule.get("fixed_minute", -1),
                    rule.get("source_text", ""),
                    json.dumps(rule.get("source_message_ids", []), ensure_ascii=False),
                    rule.get("created_by_sender_id", ""),
                    now_ts,
                    now_ts,
                ),
            )
            if conn is None:
                active.commit()

        if not previous:
            return True
        return (
            str(previous["remind_type"]) != rule["remind_type"]
            or safe_int(previous["offset_minutes"], default=0) != rule.get("offset_minutes", 0)
            or safe_int(previous["days_before"], default=0) != rule.get("days_before", 0)
            or safe_int(previous["fixed_hour"], default=-1) != rule.get("fixed_hour", -1)
            or safe_int(previous["fixed_minute"], default=-1) != rule.get("fixed_minute", -1)
        )

    def get_rule(
        self,
        group_id: str,
        category: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict | None:
        """读取某个群某个类别的提醒规则。"""
        with self.use_connection(conn) as active:
            row = active.execute(
                """
                SELECT category, remind_type, offset_minutes, days_before, fixed_hour, fixed_minute,
                       source_text, source_message_ids, created_by_sender_id
                FROM reminder_rules
                WHERE group_id=? AND category=?
                LIMIT 1
                """,
                (group_id, category),
            ).fetchone()
        return self._row_to_rule(row)

    def list_pending_deadlines_by_category(
        self,
        group_id: str,
        category: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        """列出某个群某个类别下未完成的 DDL。"""
        with self.use_connection(conn) as active:
            rows = active.execute(
                """
                SELECT id, description, category, deadline_ts, remind_at_ts, reminded, confidence
                FROM deadlines
                WHERE group_id=? AND category=? AND status='pending'
                """,
                (group_id, category),
            ).fetchall()
        return [self._row_to_deadline(row) for row in rows]

    def update_deadline_reminder(
        self,
        deadline_id: int,
        meta: dict,
        reminded: bool,
        now_ts: int,
        conn: sqlite3.Connection | None = None,
    ):
        """更新某条 DDL 的提醒时间和提醒状态。"""
        with self.use_connection(conn) as active:
            active.execute(
                """
                UPDATE deadlines
                SET remind_at_ts=?, remind_rule_type=?, remind_rule_value=?, reminded=?, updated_at=?
                WHERE id=?
                """,
                (
                    meta["remind_at_ts"],
                    meta["rule_type"],
                    meta["rule_value"],
                    1 if reminded else 0,
                    now_ts,
                    deadline_id,
                ),
            )
            if conn is None:
                active.commit()

    def deadline_exists(self, group_id: str, fingerprint: str, conn: sqlite3.Connection | None = None) -> bool:
        """按指纹判断 DDL 是否已存在。"""
        with self.use_connection(conn) as active:
            row = active.execute(
                """
                SELECT 1
                FROM deadlines
                WHERE group_id=? AND fingerprint=?
                LIMIT 1
                """,
                (group_id, fingerprint),
            ).fetchone()
        return row is not None

    def insert_deadline(
        self,
        group_id: str,
        deadline: dict,
        meta: dict,
        status: str,
        now_ts: int,
        conn: sqlite3.Connection | None = None,
    ):
        """插入一条新的 DDL 记录。"""
        with self.use_connection(conn) as active:
            active.execute(
                """
                INSERT INTO deadlines (
                    group_id,
                    description,
                    category,
                    deadline_text,
                    deadline_ts,
                    remind_at_ts,
                    remind_rule_type,
                    remind_rule_value,
                    source_message_ids,
                    source_text,
                    fingerprint,
                    status,
                    reminded,
                    reminded_at,
                    confidence,
                    extracted_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    group_id,
                    deadline["description"],
                    deadline["category"],
                    deadline["deadline_text"],
                    deadline["deadline_ts"],
                    meta["remind_at_ts"],
                    meta["rule_type"],
                    meta["rule_value"],
                    deadline["source_message_ids_json"],
                    deadline["source_text"],
                    deadline["fingerprint"],
                    status,
                    deadline["confidence"],
                    now_ts,
                    now_ts,
                ),
            )
            if conn is None:
                active.commit()

    def list_pending_deadlines(self, group_id: str, limit: int) -> list[dict]:
        """列出某个群未完成的 DDL。"""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, description, category, deadline_ts, remind_at_ts, reminded, confidence
                FROM deadlines
                WHERE group_id=? AND status='pending'
                ORDER BY deadline_ts ASC
                LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
        return [self._row_to_deadline(row) for row in rows]

    def list_pending_deadlines_due_within(self, group_id: str, end_ts: int, limit: int) -> list[dict]:
        """列出某个群在指定时间前截止的未完成 DDL。"""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, description, category, deadline_ts, remind_at_ts, reminded, confidence
                FROM deadlines
                WHERE group_id=?
                  AND status='pending'
                  AND deadline_ts<=?
                ORDER BY deadline_ts ASC
                LIMIT ?
                """,
                (group_id, end_ts, limit),
            ).fetchall()
        return [self._row_to_deadline(row) for row in rows]

    def _row_to_group(self, row: sqlite3.Row | None) -> dict | None:
        """把群表查询结果转成普通字典。"""
        if not row:
            return None
        return {
            "group_id": str(row["group_id"] or ""),
            "unified_msg_origin": str(row["unified_msg_origin"] or ""),
            "enabled": bool(int(row["enabled"])),
            "last_extract_at": safe_int(row["last_extract_at"], default=0, minimum=0),
            "last_message_row_id": safe_int(row["last_message_row_id"], default=0, minimum=0),
            "created_at": safe_int(row["created_at"], default=0, minimum=0),
            "updated_at": safe_int(row["updated_at"], default=0, minimum=0),
        }

    def _row_to_message(self, row: sqlite3.Row) -> dict:
        """把消息表查询结果转成普通字典。"""
        return {
            "id": safe_int(row["id"], default=0, minimum=0),
            "group_id": str(row["group_id"] or ""),
            "unified_msg_origin": str(row["unified_msg_origin"] or ""),
            "sender_id": str(row["sender_id"] or ""),
            "sender_name": str(row["sender_name"] or ""),
            "sender_role": str(row["sender_role"] or ""),
            "message_text": str(row["message_text"] or ""),
            "message_ts": safe_int(row["message_ts"], default=0, minimum=0),
            "is_command": bool(int(row["is_command"])),
            "is_bot": bool(int(row["is_bot"])),
            "created_at": safe_int(row["created_at"], default=0, minimum=0),
        }

    def _row_to_deadline(self, row: sqlite3.Row | None) -> dict | None:
        """把 DDL 表查询结果转成普通字典。"""
        if not row:
            return None
        return {
            "id": safe_int(row["id"], default=0, minimum=0),
            "description": str(row["description"] or ""),
            "category": str(row["category"] or "其他"),
            "deadline_ts": safe_int(row["deadline_ts"], default=0, minimum=0),
            "remind_at_ts": safe_int(row["remind_at_ts"], default=0, minimum=0),
            "reminded": bool(int(row["reminded"])),
            "confidence": float(row["confidence"] or 0),
        }

    def _row_to_rule(self, row: sqlite3.Row | None) -> dict | None:
        """把提醒规则表查询结果转成普通字典。"""
        if not row:
            return None
        return {
            "category": str(row["category"] or ""),
            "remind_type": str(row["remind_type"] or ""),
            "offset_minutes": safe_int(row["offset_minutes"], default=0, minimum=0),
            "days_before": safe_int(row["days_before"], default=0, minimum=0),
            "fixed_hour": safe_int(row["fixed_hour"], default=-1),
            "fixed_minute": safe_int(row["fixed_minute"], default=-1),
            "source_text": str(row["source_text"] or ""),
            "source_message_ids": self._decode_message_ids(row["source_message_ids"]),
            "created_by_sender_id": str(row["created_by_sender_id"] or ""),
        }

    def _row_to_reminder_dispatch(self, row: sqlite3.Row) -> dict:
        """把提醒扫描结果转成发送所需结构。"""
        return {
            "id": safe_int(row["id"], default=0, minimum=0),
            "group_id": str(row["group_id"] or ""),
            "description": str(row["description"] or ""),
            "category": str(row["category"] or "其他"),
            "deadline_ts": safe_int(row["deadline_ts"], default=0, minimum=0),
            "remind_at_ts": safe_int(row["remind_at_ts"], default=0, minimum=0),
            "unified_msg_origin": str(row["unified_msg_origin"] or ""),
        }

    def _decode_message_ids(self, raw: str | None) -> list[int]:
        """把 JSON 字符串解析成消息 ID 列表。"""
        try:
            payload = json.loads(raw or "[]")
        except Exception:
            payload = []
        if not isinstance(payload, list):
            return []

        normalized: list[int] = []
        for item in payload:
            item_id = safe_int(item, default=0, minimum=0)
            if item_id > 0:
                normalized.append(item_id)
        return normalized
