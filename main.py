import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    get_astrbot_data_path = None


@register(
    "group_deadline_reminder",
    "YourName",
    "群消息记录、总结、截止提醒插件",
    "1.0.0"
)
class GroupDeadlineReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plugin_name = "group_deadline_reminder"
        self.data_dir = self._get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / "group_deadline_reminder.db"
        self._init_db()

        self._bg_task: Optional[asyncio.Task] = None
        self._stopped = False

        try:
            self.context.add_llm_tools()
        except Exception as e:
            logger.warning(f"[group_deadline_reminder] add_llm_tools failed: {e}")

    async def initialize(self):
        self._stopped = False
        self._bg_task = asyncio.create_task(self._background_loop())
        logger.info("[group_deadline_reminder] background loop started")

    async def terminate(self):
        self._stopped = True
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except Exception:
                pass
        logger.info("[group_deadline_reminder] background loop stopped")

    # =========================
    # LLM Tool
    # =========================
    @filter.llm_tool(name="extract_group_deadlines")
    async def extract_group_deadlines(self, event: AstrMessageEvent, text: str):
        """从文本中提取截止时间和待办描述

        Args:
            text(string): 需要提取的原始文本
        """
        result = await self._extract_deadlines_by_llm(event=event, raw_text=text)
        yield event.plain_result(json.dumps(result, ensure_ascii=False, indent=2))

    # =========================
    # Commands
    # =========================
    @filter.command("ddl_on")
    async def ddl_on(self, event: AstrMessageEvent):
        """在当前群启用消息记录、总结与截止提醒"""
        if not self._is_super_admin(event):
            yield event.plain_result("权限不足，仅超级管理员可操作。")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO groups (group_id, unified_msg_origin, enabled, last_summary_at, created_at, updated_at)
                VALUES (?, ?, 1, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    unified_msg_origin=excluded.unified_msg_origin,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (group_id, self._get_unified_msg_origin(event), now, now),
            )
            conn.commit()

        yield event.plain_result(f"已在群 {group_id} 启用。")

    @filter.command("ddl_off")
    async def ddl_off(self, event: AstrMessageEvent):
        """在当前群关闭消息记录、总结与截止提醒"""
        if not self._is_super_admin(event):
            yield event.plain_result("权限不足，仅超级管理员可操作。")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                "UPDATE groups SET enabled=0, updated_at=? WHERE group_id=?",
                (now, group_id),
            )
            conn.commit()

        yield event.plain_result(f"已在群 {group_id} 关闭。")

    @filter.command("ddl_status")
    async def ddl_status(self, event: AstrMessageEvent):
        """查看当前群状态"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        with self._conn() as conn:
            row = conn.execute(
                "SELECT group_id, enabled, last_summary_at FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()

            if not row:
                yield event.plain_result("当前群尚未启用。")
                return

            msg_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE group_id=?",
                (group_id,),
            ).fetchone()[0]

            ddl_count = conn.execute(
                "SELECT COUNT(*) FROM deadlines WHERE group_id=? AND status='pending'",
                (group_id,),
            ).fetchone()[0]

        last_summary_text = (
            datetime.fromtimestamp(row[2]).strftime("%Y-%m-%d %H:%M:%S")
            if row[2] > 0 else "未执行"
        )

        yield event.plain_result(
            "群状态:\n"
            f"- group_id: {row[0]}\n"
            f"- enabled: {bool(row[1])}\n"
            f"- last_summary_at: {last_summary_text}\n"
            f"- message_count: {msg_count}\n"
            f"- pending_deadlines: {ddl_count}"
        )

    @filter.command("ddl_summary_now")
    async def ddl_summary_now(self, event: AstrMessageEvent):
        """立即对当前群执行一次总结和任务提取"""
        if not self._is_super_admin(event):
            yield event.plain_result("权限不足，仅超级管理员可操作。")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        result_text = await self._run_group_summary(group_id)
        yield event.plain_result(result_text)

    @filter.command("ddl_deadlines")
    async def ddl_deadlines(self, event: AstrMessageEvent):
        """查看当前群待处理截止事项"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, description, deadline_ts, reminded, status
                FROM deadlines
                WHERE group_id=? AND status='pending'
                ORDER BY deadline_ts ASC
                LIMIT 20
                """,
                (group_id,),
            ).fetchall()

        if not rows:
            yield event.plain_result("当前群没有待处理的截止事项。")
            return

        lines = []
        for row in rows:
            lines.append(
                f"[{row[0]}] {row[1]} | 截止: "
                f"{datetime.fromtimestamp(row[2]).strftime('%Y-%m-%d %H:%M:%S')} "
                f"| 已提醒: {bool(row[3])} | 状态: {row[4]}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("ddl_extract")
    async def ddl_extract(self, event: AstrMessageEvent):
        """手动提取文本中的截止时间和待办，用法：/ddl_extract 文本"""
        text = (event.message_str or "").strip()
        prefix = "/ddl_extract"
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

        if not text:
            yield event.plain_result("请提供要提取的文本，例如：/ddl_extract 周五下午5点前提交报价单")
            return

        result = await self._extract_deadlines_by_llm(event=event, raw_text=text)
        yield event.plain_result(json.dumps(result, ensure_ascii=False, indent=2))

    # =========================
    # Message Listener
    # =========================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群文本消息"""
        group_id = self._get_group_id(event)
        if not group_id:
            return

        if not self._group_enabled(group_id):
            return

        message_text = (event.message_str or "").strip()
        if not message_text:
            return

        if message_text.startswith("/ddl_"):
            return

        sender_id = self._get_sender_id(event)
        sender_name = self._get_sender_name(event)
        ts = self._get_event_ts(event)
        now = int(time.time())

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO messages (group_id, sender_id, sender_name, message_text, message_ts, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, sender_id, sender_name, message_text, ts, now),
            )
            conn.commit()

    # =========================
    # Background
    # =========================
    async def _background_loop(self):
        while not self._stopped:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"[group_deadline_reminder] background loop error: {e}")
            await asyncio.sleep(30)

    async def _tick(self):
        now = int(time.time())

        with self._conn() as conn:
            groups = conn.execute(
                """
                SELECT group_id, unified_msg_origin, enabled, last_summary_at
                FROM groups
                WHERE enabled=1
                """
            ).fetchall()

        for group_id, _, enabled, last_summary_at in groups:
            if not enabled:
                continue
            if self._should_run_summary(now, last_summary_at):
                try:
                    await self._run_group_summary(group_id)
                except Exception as e:
                    logger.exception(f"[group_deadline_reminder] summary failed group={group_id}: {e}")

        if self.config.get("auto_remind_enabled", True):
            await self._scan_and_remind(now)

    def _should_run_summary(self, now_ts: int, last_summary_at: int) -> bool:
        if self.config.get("enable_interval_summary", True):
            interval_minutes = int(self.config.get("summary_interval_minutes", 60) or 0)
            if interval_minutes > 0 and now_ts - last_summary_at >= interval_minutes * 60:
                return True

        if self.config.get("enable_daily_summary", True):
            daily_times = self.config.get("daily_summary_times", []) or []
            now_dt = datetime.fromtimestamp(now_ts)
            last_dt = datetime.fromtimestamp(last_summary_at) if last_summary_at > 0 else None

            for hhmm in daily_times:
                try:
                    hh, mm = hhmm.split(":")
                    target = now_dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                except Exception:
                    continue

                if now_dt >= target:
                    if last_dt is None:
                        return True
                    if last_dt.date() != now_dt.date() or last_dt < target:
                        return True

        return False

    async def _run_group_summary(self, group_id: str) -> str:
        now = int(time.time())
        lookback_minutes = int(self.config.get("summary_lookback_minutes", 120) or 120)
        max_messages = int(self.config.get("max_messages_per_summary", 200) or 200)

        with self._conn() as conn:
            group_row = conn.execute(
                "SELECT unified_msg_origin FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if not group_row:
                return f"群 {group_id} 未注册。"

            umo = group_row[0]
            since_ts = now - lookback_minutes * 60

            rows = conn.execute(
                """
                SELECT id, sender_name, message_text, message_ts
                FROM messages
                WHERE group_id=? AND message_ts>=?
                ORDER BY message_ts ASC
                LIMIT ?
                """,
                (group_id, since_ts, max_messages),
            ).fetchall()

        if not rows:
            return f"群 {group_id} 最近没有可总结的消息。"

        conversation_lines = []
        source_ids = []
        for msg_id, sender_name, message_text, message_ts in rows:
            dt_str = datetime.fromtimestamp(message_ts).strftime("%Y-%m-%d %H:%M:%S")
            conversation_lines.append(f"[{msg_id}][{dt_str}][{sender_name}] {message_text}")
            source_ids.append(msg_id)

        raw_text = "\n".join(conversation_lines)
        llm_result = await self._summarize_and_extract_for_group(
            unified_msg_origin=umo,
            group_id=group_id,
            raw_text=raw_text,
        )

        summary = (llm_result.get("summary") or "").strip()
        deadlines = llm_result.get("deadlines", [])

        inserted_count = 0
        with self._conn() as conn:
            for item in deadlines:
                desc = (item.get("description") or "").strip()
                deadline_ts = item.get("deadline_ts")
                source_text = (item.get("source_text") or "").strip()
                item_source_ids = item.get("source_message_ids") or source_ids

                if not desc or not deadline_ts:
                    continue

                try:
                    deadline_ts = int(deadline_ts)
                except Exception:
                    continue

                exists = conn.execute(
                    """
                    SELECT 1 FROM deadlines
                    WHERE group_id=? AND description=? AND deadline_ts=? AND status='pending'
                    LIMIT 1
                    """,
                    (group_id, desc, deadline_ts),
                ).fetchone()

                if exists:
                    continue

                conn.execute(
                    """
                    INSERT INTO deadlines (
                        group_id, description, deadline_ts,
                        source_message_ids, source_text,
                        status, reminded, extracted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (
                        group_id,
                        desc,
                        deadline_ts,
                        json.dumps(item_source_ids, ensure_ascii=False),
                        source_text,
                        now,
                        now,
                    ),
                )
                inserted_count += 1

            conn.execute(
                "UPDATE groups SET last_summary_at=?, updated_at=? WHERE group_id=?",
                (now, now, group_id),
            )
            conn.commit()

        if summary:
            await self._send_text_to_origin(
                umo,
                f"【群聊总结】\n{summary}\n\n本次新增截止事项：{inserted_count} 条"
            )

        return f"总结完成：新增截止事项 {inserted_count} 条。"

    async def _scan_and_remind(self, now_ts: int):
        remind_before_minutes = int(self.config.get("remind_before_minutes", 30) or 30)
        threshold_ts = now_ts + remind_before_minutes * 60

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.group_id, d.description, d.deadline_ts, g.unified_msg_origin
                FROM deadlines d
                JOIN groups g ON d.group_id = g.group_id
                WHERE g.enabled=1
                  AND d.status='pending'
                  AND d.reminded=0
                  AND d.deadline_ts<=?
                ORDER BY d.deadline_ts ASC
                """,
                (threshold_ts,),
            ).fetchall()

        for ddl_id, _, description, deadline_ts, umo in rows:
            try:
                remain_seconds = deadline_ts - now_ts
                remain_minutes = max(remain_seconds // 60, 0)
                deadline_text = datetime.fromtimestamp(deadline_ts).strftime("%Y-%m-%d %H:%M:%S")

                await self._send_text_to_origin(
                    umo,
                    "【截止提醒】\n"
                    f"事项：{description}\n"
                    f"截止时间：{deadline_text}\n"
                    f"剩余约：{remain_minutes} 分钟"
                )

                with self._conn() as conn:
                    conn.execute(
                        "UPDATE deadlines SET reminded=1, updated_at=? WHERE id=?",
                        (int(time.time()), ddl_id),
                    )
                    conn.commit()
            except Exception as e:
                logger.exception(f"[group_deadline_reminder] remind failed ddl_id={ddl_id}: {e}")

    # =========================
    # LLM
    # =========================
    async def _summarize_and_extract_for_group(
        self,
        unified_msg_origin: str,
        group_id: str,
        raw_text: str,
    ) -> Dict[str, Any]:
        provider_id = await self._resolve_provider_id(unified_msg_origin)

        prompt = f"""
你是一个群聊任务整理助手。请根据下面的群消息记录，完成两件事：

1. 生成一段简洁的群聊总结。
2. 提取所有明确或高置信度的待办事项，尤其是带有截止时间/截止日期/时间要求的事项。

输出必须是 JSON，对象结构如下：
{{
  "summary": "字符串",
  "deadlines": [
    {{
      "description": "待办描述，简洁明确",
      "deadline_text": "原文中的时间表达",
      "deadline_ts": 1719999999,
      "source_text": "触发该任务的原始文本",
      "source_message_ids": [1,2,3]
    }}
  ]
}}

要求：
- 只输出 JSON，不要输出额外解释。
- 如果没有可提取事项，deadlines 返回 []。
- deadline_ts 必须是 Unix 时间戳（秒）。
- 当前服务器时间戳参考：{int(time.time())}
- 当前群 ID：{group_id}

群消息如下：
{raw_text}
""".strip()

        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )

        text = (getattr(llm_resp, "completion_text", "") or "").strip()
        return self._safe_json_loads(text, default={"summary": "", "deadlines": []})

    async def _extract_deadlines_by_llm(self, event: AstrMessageEvent, raw_text: str) -> Dict[str, Any]:
        provider_id = await self._resolve_provider_id(self._get_unified_msg_origin(event))

        prompt = f"""
你是一个文本任务抽取器。请从输入文本中提取所有待办事项及其截止时间。

输出必须为 JSON：
{{
  "deadlines": [
    {{
      "description": "待办描述",
      "deadline_text": "原始时间表达",
      "deadline_ts": 1719999999,
      "confidence": 0.95
    }}
  ]
}}

要求：
- 只输出 JSON。
- 没有提取到则返回 {{"deadlines":[]}}。
- deadline_ts 为 Unix 时间戳（秒）。
- 当前服务器时间戳参考：{int(time.time())}

输入文本：
{raw_text}
""".strip()

        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )

        text = (getattr(llm_resp, "completion_text", "") or "").strip()
        return self._safe_json_loads(text, default={"deadlines": []})

    async def _resolve_provider_id(self, unified_msg_origin: str) -> str:
        cfg_provider = (self.config.get("summary_model_provider_id", "") or "").strip()
        if cfg_provider:
            return cfg_provider

        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if getter:
            try:
                return await getter(umo=unified_msg_origin)
            except TypeError:
                try:
                    return await getter(unified_msg_origin)
                except Exception:
                    pass
            except Exception:
                pass

        raise RuntimeError("无法获取 chat_provider_id，请在配置中填写 summary_model_provider_id")

    # =========================
    # DB
    # =========================
    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    unified_msg_origin TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_summary_at INTEGER NOT NULL DEFAULT 0,
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
                    sender_id TEXT,
                    sender_name TEXT,
                    message_text TEXT NOT NULL,
                    message_ts INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
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
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    deadline_ts INTEGER NOT NULL,
                    source_message_ids TEXT DEFAULT '',
                    source_text TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    reminded INTEGER NOT NULL DEFAULT 0,
                    extracted_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_deadlines_group_deadline
                ON deadlines(group_id, deadline_ts, status, reminded)
                """
            )
            conn.commit()

    # =========================
    # Helpers
    # =========================
    def _get_data_dir(self) -> Path:
        if get_astrbot_data_path:
            return get_astrbot_data_path() / "plugin_data" / self.plugin_name
        return Path("data") / "plugin_data" / self.plugin_name

    def _group_enabled(self, group_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
        return bool(row and row[0] == 1)

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.message_obj.group_id or "")
        except Exception:
            return str(getattr(event, "group_id", "") or "")

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        try:
            sender = event.message_obj.sender
            return str(getattr(sender, "user_id", "") or getattr(sender, "id", "") or "")
        except Exception:
            return ""

    def _get_sender_name(self, event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name()
        except Exception:
            try:
                sender = event.message_obj.sender
                return str(getattr(sender, "nickname", "") or getattr(sender, "name", "") or "")
            except Exception:
                return ""

    def _get_event_ts(self, event: AstrMessageEvent) -> int:
        try:
            ts = int(event.message_obj.timestamp)
            if ts > 0:
                return ts
        except Exception:
            pass
        return int(time.time())

    def _get_unified_msg_origin(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _is_super_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = self._get_sender_id(event)
        admins = self.config.get("super_admin_ids", []) or []
        return str(sender_id) in {str(x) for x in admins}

    async def _send_text_to_origin(self, unified_msg_origin: str, text: str):
        if not unified_msg_origin:
            raise RuntimeError("unified_msg_origin 为空，无法主动发送消息")

        try:
            from astrbot.api.message_components import Plain
            await self.context.send_message(unified_msg_origin, [Plain(text)])
            return
        except Exception:
            pass

        try:
            from astrbot.api.event import MessageChain
            await self.context.send_message(unified_msg_origin, MessageChain().message(text))
            return
        except Exception:
            pass

        raise RuntimeError("发送消息失败，请按你的 AstrBot 版本调整 send_message 的消息链构造方式")

    def _safe_json_loads(self, text: str, default: Dict[str, Any]) -> Dict[str, Any]:
        text = text.strip()
        if not text:
            return default

        if text.startswith("```"):
            first_newline = text.find("\n")
            last_fence = text.rfind("```")
            if first_newline != -1 and last_fence != -1 and last_fence > first_newline:
                text = text[first_newline + 1:last_fence].strip()

        try:
            return json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    pass
        return default
