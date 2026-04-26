"""Single-file ddl tracker plugin for debugging and incremental development."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from time import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


STATE_FILE = Path(__file__).with_name("ddl_groups.json")
MAX_MESSAGES_PER_GROUP = 500
DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES = 30
DEFAULT_REMIND_BEFORE_MINUTES = 60
AUTO_LOOP_TICK_SECONDS = 15
COMMAND_NAMES = {"/ddl_on", "/ddl_off", "/ddl_status", "/ddl_extract", "/ddl_nearest"}
DEFAULT_EXTRACT_PROMPT = """
你是一个负责提取群聊中 DDL 的助手。
请从群消息里识别明确提到的作业、考试、实验、报告、报名、提交截止等事项。
只输出 JSON，不要输出解释，不要输出 Markdown 代码块。
输出必须是一个对象，包含 summary 和 items 两个字段。
items 是数组；每个元素包含：message_index、type、title、deadline_text、normalized_deadline、source_text。
如果没有明确 DDL，请返回 {"summary":"未识别到明确 DDL","items":[]}。
不要编造不存在的信息，deadline_text 必须直接基于原消息。
要特别识别相对时间表达，例如“一小时后截止”“今晚 11 点前”“一周内提交”“下周一前”。
如果消息里出现相对时间，请优先根据该条消息前面的 ts/time 字段来解析。
如果能推算出明确时间，请填写 normalized_deadline；不能精确到分钟时，也尽量给出最合理的截止时间文本。
如果某条消息只是设置提醒规则，例如“作业提前1天提醒”“考试前一天晚上22点提醒”，这是提醒规则，不是 DDL，不要加入 items。
尽量把 type 归一化成稳定类别，例如作业、考试、实验、报告、讲座、报名、项目、论文。
""".strip()


def _load_state() -> dict[str, dict]:
    """Load persisted state from a tiny json file."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, dict]):
    """Persist current state."""
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _safe_json_loads(text: str) -> dict:
    """Try to parse model output into a json object."""
    raw = str(text or "").strip()
    if not raw:
        return {}

    if raw.startswith("```"):
        first_newline = raw.find("\n")
        last_fence = raw.rfind("```")
        if first_newline != -1 and last_fence != -1 and last_fence > first_newline:
            raw = raw[first_newline + 1:last_fence].strip()

    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                payload = json.loads(raw[start:end + 1])
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
    return {}


def _safe_int(value, default: int = 0, minimum: int | None = None) -> int:
    """Convert a value to int with a simple lower-bound clamp."""
    try:
        result = int(value)
    except Exception:
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    return result


def _format_ts(timestamp: int) -> str:
    """Format unix timestamp for user-facing status and prompts."""
    ts = _safe_int(timestamp, default=0, minimum=0)
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_item(item: dict) -> dict | None:
    """Normalize a parsed ddl item returned by the model."""
    if not isinstance(item, dict):
        return None

    normalized = {
        "message_index": _safe_int(item.get("message_index"), default=0, minimum=0),
        "type": str(item.get("type") or "").strip() or "其他",
        "title": str(item.get("title") or "").strip(),
        "deadline_text": str(item.get("deadline_text") or "").strip(),
        "normalized_deadline": str(item.get("normalized_deadline") or "").strip(),
        "source_text": str(item.get("source_text") or "").strip(),
    }
    if not normalized["title"]:
        return None
    return normalized


def _build_fingerprint(item: dict) -> str:
    """Generate a stable fingerprint for deduplication."""
    normalized_deadline = item.get("normalized_deadline") or item.get("deadline_text") or ""
    raw = "|".join(
        [
            str(item.get("type") or "").strip().lower(),
            str(item.get("title") or "").strip().lower(),
            str(normalized_deadline).strip().lower(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@register(
    "ddl_tracker",
    "Codex",
    "单文件 DDL 调试插件",
    "0.8.0",
)
class DDLTrackerPlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(context)
        self.config = config or {}
        self.state = _load_state()
        if self._normalize_loaded_state():
            self._persist()
        self._running = True
        self._extracting_groups: set[str] = set()
        self._auto_task = asyncio.create_task(self._auto_extract_loop())
        logger.info(
            "[ddl_tracker] loaded config=%s state=%s",
            json.dumps(self._dump_config(), ensure_ascii=False),
            json.dumps(self.state, ensure_ascii=False),
        )

    @filter.command("ddl_on")
    async def ddl_on(self, event: AstrMessageEvent):
        """开启当前群的 DDL 跟踪。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._ensure_group_state(group_id, event)
        group_state["enabled"] = True
        self._persist()
        logger.info("[ddl_tracker] ddl_on group=%s", group_id)
        yield event.plain_result(f"已开启 DDL，group_id={group_id}")

    @filter.command("ddl_off")
    async def ddl_off(self, event: AstrMessageEvent):
        """关闭当前群的 DDL 跟踪。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._ensure_group_state(group_id, event)
        group_state["enabled"] = False
        self._persist()
        logger.info("[ddl_tracker] ddl_off group=%s", group_id)
        yield event.plain_result(f"已关闭 DDL，group_id={group_id}")

    @filter.command("ddl_status")
    async def ddl_status(self, event: AstrMessageEvent):
        """显示当前群状态、消息数和去重后的 DDL 数。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._prepare_group_state_for_read(group_id)
        enabled = bool(group_state.get("enabled", False))
        message_count = len(group_state.get("messages", []))
        ddl_count = len(group_state.get("ddl_items", []))
        rule_count = len(self._list_reminder_rules(group_state))
        future_task_count = sum(
            1
            for item in group_state.get("ddl_items", [])
            if isinstance(item, dict) and str(item.get("future_task_name") or "").strip()
        )
        last_extract_at = _format_ts(group_state.get("last_extract_at", 0))
        status_text = "开启" if enabled else "关闭"
        auto_text = "开启" if self._auto_extract_enabled() else "关闭"
        remind_text = "开启" if self._auto_remind_enabled() else "关闭"
        yield event.plain_result(
            f"当前群 DDL 状态：{status_text}\n"
            f"group_id={group_id}\n"
            f"已记录消息数={message_count}\n"
            f"去重后 DDL 数={ddl_count}\n"
            f"分类提醒规则数={rule_count}\n"
            f"已记录官方任务数={future_task_count}\n"
            f"自动整理={auto_text}\n"
            f"自动整理周期={self._auto_extract_interval_minutes()} 分钟\n"
            f"自动提醒={remind_text}\n"
            f"提醒提前={self._remind_before_minutes()} 分钟\n"
            f"提醒后端=主 Agent future_task\n"
            f"上次整理时间={last_extract_at}\n"
            f"手动整理命令=/ddl_extract [分钟]\n"
            f"最近截止命令=/ddl_nearest [数量]"
        )

    @filter.command("ddl_extract")
    async def ddl_extract(self, event: AstrMessageEvent):
        """手动整理最近一段时间消息中的 DDL。"""
        async for result in self._handle_extract_command(event, command_name="/ddl_extract"):
            yield result

    @filter.command("ddl_nearest")
    async def ddl_nearest(self, event: AstrMessageEvent):
        """显示距离截止最近的 K 个 DDL。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._prepare_group_state_for_read(group_id)
        if not bool(group_state.get("enabled", False)):
            yield event.plain_result("当前群尚未开启 DDL，请先执行 /ddl_on")
            return

        limit = self._parse_limit_arg(event.message_str, default=5)
        nearest_items = self._get_nearest_ddls(group_state, limit)
        if not nearest_items:
            yield event.plain_result("当前没有可排序的未截止 DDL。")
            return

        now_ts = int(time())
        lines = [f"最近的 {len(nearest_items)} 个 DDL："]
        for index, item in enumerate(nearest_items, start=1):
            remain_text = self._format_remaining(item["deadline_ts"] - now_ts)
            lines.append(
                f"{index}. {item['title']} | {item['type']} | "
                f"截止={item['normalized_deadline']} | 剩余={remain_text}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="ddl_extract_recent_messages")
    async def ddl_extract_recent_messages_tool(self, event: AstrMessageEvent, minutes: int = 0):
        '''整理最近一段时间的群消息并提取 DDL。

        Args:
            minutes(number): 要整理最近多少分钟的消息；填 0 时使用插件默认自动整理周期。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._prepare_group_state_for_read(group_id)
        if not bool(group_state.get("enabled", False)):
            return json.dumps({"ok": False, "reason": "当前群尚未开启 DDL"}, ensure_ascii=False)

        lookback_minutes = _safe_int(minutes, default=self._auto_extract_interval_minutes(), minimum=1)
        result = await self._extract_group_ddls(
            group_id=group_id,
            group_state=group_state,
            unified_msg_origin=str(event.unified_msg_origin or group_state.get("unified_msg_origin") or ""),
            lookback_minutes=lookback_minutes,
            source="tool",
        )
        payload = {
            "ok": True,
            "group_id": group_id,
            "lookback_minutes": lookback_minutes,
            "provider_id": result.get("provider_id", ""),
            "message_count": result.get("message_count", 0),
            "extracted_count": result.get("extracted_count", 0),
            "added_count": result.get("added_count", 0),
            "updated_count": result.get("updated_count", 0),
            "ddl_total_count": len(self.state.get(group_id, {}).get("ddl_items", [])),
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_get_remaining")
    async def ddl_get_remaining_tool(self, event: AstrMessageEvent, limit: int = 10):
        '''查看当前群尚未截止的 DDL。

        Args:
            limit(number): 最多返回多少条 DDL。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._prepare_group_state_for_read(group_id)
        items = self._get_nearest_ddls(group_state, _safe_int(limit, default=10, minimum=1))
        payload = {
            "ok": True,
            "group_id": group_id,
            "count": len(items),
            "items": [self._tool_item_payload(item) for item in items],
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_get_due_within")
    async def ddl_get_due_within_tool(self, event: AstrMessageEvent, hours: int = 24):
        '''查看指定时间范围内即将截止的 DDL。

        Args:
            hours(number): 未来多少小时内截止。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._prepare_group_state_for_read(group_id)
        items = self._get_due_within_ddls(group_state, _safe_int(hours, default=24, minimum=1))
        payload = {
            "ok": True,
            "group_id": group_id,
            "hours": _safe_int(hours, default=24, minimum=1),
            "count": len(items),
            "items": [self._tool_item_payload(item) for item in items],
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_set_type_reminder")
    async def ddl_set_type_reminder_tool(self, event: AstrMessageEvent, ddl_type: str, rule_text: str):
        '''为某一类 DDL 设置提醒规则。

        Args:
            ddl_type(string): DDL 类型关键词，例如作业、考试、讲座报告。
            rule_text(string): 提醒规则文本，例如“提前1天提醒”“前一天晚上22点提醒”。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._ensure_group_state(group_id, event)
        rule = self._build_reminder_rule_from_parts(ddl_type=ddl_type, rule_text=rule_text)
        if not rule:
            payload = {
                "ok": False,
                "group_id": group_id,
                "reason": "无法解析提醒规则",
                "ddl_type": str(ddl_type or ""),
                "rule_text": str(rule_text or ""),
            }
            return json.dumps(payload, ensure_ascii=False)

        self._upsert_reminder_rule(group_state, rule)
        self.state[group_id] = group_state
        self._persist()
        payload = {
            "ok": True,
            "group_id": group_id,
            "rule": self._serialize_reminder_rule(rule),
            "pending_future_task_count": len(
                self._get_pending_future_tasks(group_id, group_state, limit=9999)
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_get_reminder_rules")
    async def ddl_get_reminder_rules_tool(self, event: AstrMessageEvent):
        '''查看当前群已生效的分类提醒规则。'''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._prepare_group_state_for_read(group_id)
        rules = self._list_reminder_rules(group_state)
        payload = {
            "ok": True,
            "group_id": group_id,
            "count": len(rules),
            "rules": [self._serialize_reminder_rule(rule) for rule in rules],
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_get_pending_future_tasks")
    async def ddl_get_pending_future_tasks_tool(self, event: AstrMessageEvent, limit: int = 20):
        '''获取当前群需要由主 Agent 创建的官方 FutureTask 提醒计划。

        Args:
            limit(number): 最多返回多少个待创建的提醒任务。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        if not self._auto_remind_enabled():
            return json.dumps(
                {"ok": False, "reason": "当前插件已关闭自动提醒配置"},
                ensure_ascii=False,
            )

        group_state = self._prepare_group_state_for_read(group_id)
        if not bool(group_state.get("enabled", False)):
            return json.dumps({"ok": False, "reason": "当前群尚未开启 DDL"}, ensure_ascii=False)
        items = self._get_pending_future_tasks(
            group_id=group_id,
            group_state=group_state,
            limit=_safe_int(limit, default=20, minimum=1),
        )
        payload = {
            "ok": True,
            "group_id": group_id,
            "count": len(items),
            "items": items,
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_mark_future_task_created")
    async def ddl_mark_future_task_created_tool(
        self,
        event: AstrMessageEvent,
        fingerprint: str,
        remind_key: str,
        task_name: str,
    ):
        '''在调用官方 future_task 创建成功后，记录该 DDL 已绑定官方任务。

        Args:
            fingerprint(string): DDL 指纹，由 ddl_get_pending_future_tasks 返回。
            remind_key(string): 提醒规则键，由 ddl_get_pending_future_tasks 返回。
            task_name(string): 实际创建时使用的 future_task 任务名。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._prepare_group_state_for_read(group_id)
        if not bool(group_state.get("enabled", False)):
            return json.dumps({"ok": False, "reason": "当前群尚未开启 DDL"}, ensure_ascii=False)
        fingerprint_text = str(fingerprint or "").strip()
        remind_key_text = str(remind_key or "").strip()
        task_name_text = str(task_name or "").strip()
        if not fingerprint_text or not remind_key_text or not task_name_text:
            return json.dumps(
                {"ok": False, "reason": "fingerprint、remind_key、task_name 不能为空"},
                ensure_ascii=False,
            )

        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("fingerprint") or "").strip() != fingerprint_text:
                continue

            deadline_ts = self._item_deadline_ts(item)
            remind_plan = self._get_item_remind_plan(group_state, item, deadline_ts)
            current_remind_key = str(remind_plan.get("remind_key") or "")
            current_remind_ts = _safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
            if current_remind_key != remind_key_text:
                payload = {
                    "ok": False,
                    "reason": "当前 DDL 的提醒计划已变化，请重新获取待创建任务",
                    "group_id": group_id,
                    "fingerprint": fingerprint_text,
                    "current_remind_key": current_remind_key,
                    "requested_remind_key": remind_key_text,
                }
                return json.dumps(payload, ensure_ascii=False)

            item["future_task_name"] = task_name_text
            item["future_task_remind_key"] = current_remind_key
            item["future_task_remind_ts"] = current_remind_ts
            item["future_task_recorded_at"] = int(time())
            self.state[group_id] = group_state
            self._persist()
            payload = {
                "ok": True,
                "group_id": group_id,
                "fingerprint": fingerprint_text,
                "task_name": task_name_text,
                "remind_at": _format_ts(current_remind_ts),
            }
            return json.dumps(payload, ensure_ascii=False)

        return json.dumps(
            {
                "ok": False,
                "reason": "未找到对应 DDL",
                "group_id": group_id,
                "fingerprint": fingerprint_text,
            },
            ensure_ascii=False,
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录已开启群中的纯文本消息。"""
        group_id = self._get_group_id(event)
        if not group_id:
            return

        group_state = self.state.get(group_id)
        if not group_state or not bool(group_state.get("enabled", False)):
            return

        group_state["unified_msg_origin"] = str(event.unified_msg_origin or "")

        message_text = str(event.message_str or "").strip()
        if not message_text:
            return
        if self._is_plugin_command_text(message_text):
            return

        reminder_rule = self._extract_reminder_rule_from_text(message_text)
        if reminder_rule:
            self._upsert_reminder_rule(group_state, reminder_rule)
            logger.info(
                "[ddl_tracker] auto rule captured group=%s rule=%s",
                group_id,
                json.dumps(self._serialize_reminder_rule(reminder_rule), ensure_ascii=False),
            )

        messages = group_state.setdefault("messages", [])
        messages.append(
            {
                "sender_name": str(event.get_sender_name() or ""),
                "sender_id": str(event.get_sender_id() or ""),
                "message_text": message_text,
                "message_ts": int(time()),
            }
        )
        if len(messages) > MAX_MESSAGES_PER_GROUP:
            del messages[:-MAX_MESSAGES_PER_GROUP]
        self.state[group_id] = group_state
        self._persist()

    async def terminate(self):
        """Plugin shutdown hook."""
        self._running = False
        if self._auto_task:
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception("[ddl_tracker] terminate auto task failed: %s", exc)
        logger.info("[ddl_tracker] terminate called")

    async def _handle_extract_command(self, event: AstrMessageEvent, command_name: str):
        """Shared logic for manual extraction commands."""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._prepare_group_state_for_read(group_id)
        if not bool(group_state.get("enabled", False)):
            yield event.plain_result("当前群尚未开启 DDL，请先执行 /ddl_on")
            return

        lookback_minutes = self._parse_minutes_arg(
            event.message_str,
            default=self._auto_extract_interval_minutes(),
        )
        result = await self._extract_group_ddls(
            group_id=group_id,
            group_state=group_state,
            unified_msg_origin=str(event.unified_msg_origin or group_state.get("unified_msg_origin") or ""),
            lookback_minutes=lookback_minutes,
            source="manual",
        )
        payload = {
            "command": command_name,
            "group_id": group_id,
            "lookback_minutes": lookback_minutes,
            "provider_id": result.get("provider_id", ""),
            "message_count": result.get("message_count", 0),
            "extracted_count": result.get("extracted_count", 0),
            "added_count": result.get("added_count", 0),
            "updated_count": result.get("updated_count", 0),
            "ddl_total_count": len(self.state.get(group_id, {}).get("ddl_items", [])),
            "parsed_result": result.get("parsed_result", {}),
            "raw_result": result.get("raw_result", ""),
        }
        logger.info(
            "[ddl_tracker] manual extract payload=%s",
            json.dumps(payload, ensure_ascii=False),
        )
        yield event.plain_result(
            f"手动整理完成：提取 {payload['extracted_count']} 条，"
            f"新增 {payload['added_count']} 条，更新 {payload['updated_count']} 条。"
        )

    async def _auto_extract_loop(self):
        """Background loop that cleans expired items and runs auto extraction."""
        while self._running:
            try:
                removed_count = self._purge_expired_from_all_groups()
                if removed_count > 0:
                    logger.info("[ddl_tracker] auto purged expired ddls=%s", removed_count)
                    self._persist()

                if self._auto_extract_enabled():
                    await self._run_auto_extract_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[ddl_tracker] auto extract loop failed: %s", exc)

            try:
                await asyncio.sleep(AUTO_LOOP_TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _run_auto_extract_once(self):
        """Iterate groups and trigger auto extraction when interval has elapsed."""
        now_ts = int(time())
        interval_minutes = self._auto_extract_interval_minutes()
        interval_seconds = interval_minutes * 60

        for group_id, group_state in list(self.state.items()):
            self._ensure_group_state_fields(group_state)
            if not bool(group_state.get("enabled", False)):
                continue
            if group_id in self._extracting_groups:
                continue

            unified_msg_origin = str(group_state.get("unified_msg_origin") or "")
            if not unified_msg_origin:
                continue

            last_extract_at = _safe_int(group_state.get("last_extract_at"), default=0, minimum=0)
            if last_extract_at > 0 and now_ts - last_extract_at < interval_seconds:
                continue

            await self._extract_group_ddls(
                group_id=group_id,
                group_state=group_state,
                unified_msg_origin=unified_msg_origin,
                lookback_minutes=interval_minutes,
                source="auto",
            )

    async def _extract_group_ddls(
        self,
        group_id: str,
        group_state: dict,
        unified_msg_origin: str,
        lookback_minutes: int,
        source: str,
    ) -> dict:
        """Extract ddls from recent messages and persist deduplicated results."""
        self._extracting_groups.add(group_id)
        try:
            self._ensure_group_state_fields(group_state)
            self._purge_expired_ddls(group_state)
            prompt_messages = self._select_recent_messages(
                group_state.get("messages", []),
                lookback_minutes=lookback_minutes,
            )
            if not prompt_messages:
                result = {
                    "provider_id": "",
                    "message_count": 0,
                    "extracted_count": 0,
                    "added_count": 0,
                    "updated_count": 0,
                    "parsed_result": {"summary": "没有可供整理的消息", "items": []},
                    "raw_result": "",
                }
                self._update_extract_meta(group_state, source, result)
                self.state[group_id] = group_state
                self._persist()
                return result

            provider_id = await self._get_provider_id(unified_msg_origin)
            if not provider_id:
                result = {
                    "provider_id": "",
                    "message_count": len(prompt_messages),
                    "extracted_count": 0,
                    "added_count": 0,
                    "updated_count": 0,
                    "parsed_result": {"summary": "没有可用的模型 provider", "items": []},
                    "raw_result": "",
                }
                self._update_extract_meta(group_state, source, result)
                self.state[group_id] = group_state
                self._persist()
                return result

            prompt = self._build_ai_extract_prompt(prompt_messages)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            raw_result = str(getattr(llm_resp, "completion_text", "") or "").strip()
            parsed_result = _safe_json_loads(raw_result)
            extracted_count = len(parsed_result.get("items") or [])
            added_count, updated_count = self._merge_ddl_items(
                group_state=group_state,
                parsed_result=parsed_result,
            )
            self._purge_expired_ddls(group_state)
            result = {
                "provider_id": provider_id,
                "message_count": len(prompt_messages),
                "extracted_count": extracted_count,
                "added_count": added_count,
                "updated_count": updated_count,
                "parsed_result": parsed_result,
                "raw_result": raw_result,
            }
            self._update_extract_meta(group_state, source, result)
            self.state[group_id] = group_state
            self._persist()
            logger.info(
                "[ddl_tracker] %s extract group=%s provider=%s message_count=%s extracted=%s added=%s updated=%s",
                source,
                group_id,
                provider_id,
                len(prompt_messages),
                extracted_count,
                added_count,
                updated_count,
            )
            return result
        except Exception as exc:
            logger.exception("[ddl_tracker] %s extract failed group=%s: %s", source, group_id, exc)
            result = {
                "provider_id": "",
                "message_count": 0,
                "extracted_count": 0,
                "added_count": 0,
                "updated_count": 0,
                "parsed_result": {"summary": f"{source} 提取失败", "items": []},
                "raw_result": str(exc),
            }
            self._update_extract_meta(group_state, source, result)
            self.state[group_id] = group_state
            self._persist()
            return result
        finally:
            self._extracting_groups.discard(group_id)

    def _update_extract_meta(self, group_state: dict, source: str, result: dict):
        """Update extraction bookkeeping fields in group state."""
        group_state["last_extract_at"] = int(time())
        group_state["last_extract_source"] = source
        group_state["last_extract_result"] = {
            "provider_id": result.get("provider_id", ""),
            "message_count": result.get("message_count", 0),
            "extracted_count": result.get("extracted_count", 0),
            "added_count": result.get("added_count", 0),
            "updated_count": result.get("updated_count", 0),
            "parsed_result": result.get("parsed_result", {}),
            "raw_result": result.get("raw_result", ""),
        }

    def _merge_ddl_items(self, group_state: dict, parsed_result: dict) -> tuple[int, int]:
        """Merge newly extracted ddl items into persistent deduplicated state."""
        existing_items = group_state.setdefault("ddl_items", [])
        existing_by_fp = {
            str(item.get("fingerprint") or ""): item
            for item in existing_items
            if str(item.get("fingerprint") or "")
        }
        added_count = 0
        updated_count = 0

        for raw_item in parsed_result.get("items") or []:
            item = _normalize_item(raw_item)
            if not item:
                continue

            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts > 0 and deadline_ts <= int(time()):
                continue

            fingerprint = _build_fingerprint(item)
            item["fingerprint"] = fingerprint
            item["deadline_ts"] = deadline_ts
            item["updated_at"] = int(time())

            existing = existing_by_fp.get(fingerprint)
            if existing is None:
                item["created_at"] = item["updated_at"]
                item["last_reminded_at"] = 0
                item["last_reminded_deadline_ts"] = 0
                item["last_reminded_key"] = ""
                item["future_task_name"] = ""
                item["future_task_remind_key"] = ""
                item["future_task_remind_ts"] = 0
                item["future_task_recorded_at"] = 0
                existing_items.append(item)
                existing_by_fp[fingerprint] = item
                added_count += 1
                continue

            merged = dict(existing)
            for key in ("type", "title", "deadline_text", "normalized_deadline", "source_text", "message_index"):
                new_value = item.get(key)
                old_value = merged.get(key)
                if new_value and (not old_value or len(str(new_value)) >= len(str(old_value))):
                    merged[key] = new_value
            merged_deadline_ts = self._item_deadline_ts(merged)
            merged["deadline_ts"] = merged_deadline_ts
            if _safe_int(merged.get("last_reminded_deadline_ts"), default=0, minimum=0) != merged_deadline_ts:
                merged["last_reminded_at"] = 0
                merged["last_reminded_deadline_ts"] = 0
                merged["last_reminded_key"] = ""
            merged.setdefault("future_task_name", "")
            merged.setdefault("future_task_remind_key", "")
            merged.setdefault("future_task_remind_ts", 0)
            merged.setdefault("future_task_recorded_at", 0)
            merged["updated_at"] = item["updated_at"]

            if merged != existing:
                existing.clear()
                existing.update(merged)
                updated_count += 1

        group_state["ddl_items"] = existing_items
        return added_count, updated_count

    def _select_recent_messages(self, messages: list[dict], lookback_minutes: int) -> list[dict]:
        """Select recent recorded messages inside the requested time window."""
        if not messages:
            return []

        cutoff_ts = int(time()) - lookback_minutes * 60
        result = []
        for item in messages:
            text = str(item.get("message_text") or "").strip()
            if not text:
                continue
            if self._is_plugin_command_text(text):
                continue
            if _safe_int(item.get("message_ts"), default=0, minimum=0) < cutoff_ts:
                continue
            result.append(item)
        return result

    def _normalize_loaded_state(self) -> bool:
        """Ensure loaded state matches the current single-file schema."""
        changed = False
        for group_id, group_state in list(self.state.items()):
            if not isinstance(group_state, dict):
                self.state[group_id] = {
                    "enabled": False,
                    "messages": [],
                    "ddl_items": [],
                    "reminder_rules": {},
                }
                changed = True
                continue

            if self._ensure_group_state_fields(group_state):
                changed = True
            if self._purge_expired_ddls(group_state) > 0:
                changed = True
        return changed

    def _prepare_group_state_for_read(self, group_id: str) -> dict:
        """Prepare a group state before read-only commands like status/nearest."""
        group_state = self.state.get(group_id, {})
        if not isinstance(group_state, dict):
            group_state = {}

        changed = self._ensure_group_state_fields(group_state)
        removed_count = self._purge_expired_ddls(group_state)
        if group_id in self.state and (changed or removed_count > 0):
            self.state[group_id] = group_state
            self._persist()
        return group_state

    def _ensure_group_state_fields(self, group_state: dict) -> bool:
        """Ensure minimal persistent keys exist for a group state."""
        changed = False
        if not isinstance(group_state.get("messages"), list):
            group_state["messages"] = []
            changed = True
        if not isinstance(group_state.get("ddl_items"), list):
            group_state["ddl_items"] = []
            changed = True
        if not isinstance(group_state.get("reminder_rules"), dict):
            group_state["reminder_rules"] = {}
            changed = True
        if "enabled" not in group_state:
            group_state["enabled"] = False
            changed = True
        if "unified_msg_origin" not in group_state:
            group_state["unified_msg_origin"] = ""
            changed = True
        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            if "future_task_name" not in item:
                item["future_task_name"] = ""
                changed = True
            if "future_task_remind_key" not in item:
                item["future_task_remind_key"] = ""
                changed = True
            if "future_task_remind_ts" not in item:
                item["future_task_remind_ts"] = 0
                changed = True
            if "future_task_recorded_at" not in item:
                item["future_task_recorded_at"] = 0
                changed = True
        return changed

    def _purge_expired_from_all_groups(self) -> int:
        """Remove expired ddl items from every group."""
        removed_count = 0
        for group_state in self.state.values():
            if not isinstance(group_state, dict):
                continue
            self._ensure_group_state_fields(group_state)
            removed_count += self._purge_expired_ddls(group_state)
        return removed_count

    def _purge_expired_ddls(self, group_state: dict) -> int:
        """Remove ddl items whose deadlines have already passed."""
        items = group_state.get("ddl_items", [])
        if not isinstance(items, list) or not items:
            return 0

        now_ts = int(time())
        kept_items = []
        removed_count = 0
        for item in items:
            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts > 0 and deadline_ts <= now_ts:
                removed_count += 1
                continue
            kept_items.append(item)

        if removed_count > 0:
            group_state["ddl_items"] = kept_items
        return removed_count

    def _extract_reminder_rule_from_text(self, raw_message: str) -> dict | None:
        """Parse a natural-language type-specific reminder rule from a group message."""
        message = str(raw_message or "").strip()
        if not message:
            return None

        compact = re.sub(r"\s+", "", message)
        if "提醒" not in compact and "通知" not in compact:
            return None

        relative_match = re.search(
            r"(?P<ddl_type>[\u4e00-\u9fa5A-Za-z0-9]{1,20})(?:类)?(?:DDL)?(?:都|统一|全部)?提前"
            r"(?P<value>[0-9零一二两三四五六七八九十百半]+)"
            r"(?P<unit>分钟|小时|天|周|礼拜)(?:前)?(?:提醒|通知)",
            compact,
        )
        if relative_match:
            return self._build_relative_rule(
                ddl_type=relative_match.group("ddl_type"),
                value_text=relative_match.group("value"),
                unit_text=relative_match.group("unit"),
                rule_text=relative_match.group(0),
            )

        fixed_match = re.search(
            r"(?P<ddl_type>[\u4e00-\u9fa5A-Za-z0-9]{1,20})(?:类)?(?:DDL)?(?:都|统一|全部)?"
            r"(?:(?:前(?P<days>[0-9零一二两三四五六七八九十]+)天)|(?P<one_day>前一天)|(?P<same_day>当天))"
            r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
            r"(?P<hour>[0-9]{1,2})"
            r"(?:[点时:：](?P<minute>[0-9]{1,2}))?"
            r"(?:分)?(?:提醒|通知)",
            compact,
        )
        if fixed_match:
            if fixed_match.group("same_day"):
                days_before = 0
            elif fixed_match.group("one_day"):
                days_before = 1
            else:
                days_before = self._parse_number_token(fixed_match.group("days"))

            return self._build_fixed_clock_rule(
                ddl_type=fixed_match.group("ddl_type"),
                days_before=days_before,
                hour_text=fixed_match.group("hour"),
                minute_text=fixed_match.group("minute"),
                period_text=fixed_match.group("period"),
                rule_text=fixed_match.group(0),
            )
        return None

    def _build_reminder_rule_from_parts(self, ddl_type: str, rule_text: str) -> dict | None:
        """Build a reminder rule from tool arguments."""
        type_text = str(ddl_type or "").strip()
        rule_part = str(rule_text or "").strip()
        if not type_text or not rule_part:
            return None

        combined = f"{type_text}{rule_part}"
        rule = self._extract_reminder_rule_from_text(combined)
        if rule:
            return rule

        compact = re.sub(r"\s+", "", rule_part)
        relative_match = re.fullmatch(
            r"提前(?P<value>[0-9零一二两三四五六七八九十百半]+)(?P<unit>分钟|小时|天|周|礼拜)(?:前)?(?:提醒|通知)?",
            compact,
        )
        if relative_match:
            return self._build_relative_rule(
                ddl_type=type_text,
                value_text=relative_match.group("value"),
                unit_text=relative_match.group("unit"),
                rule_text=rule_part,
            )

        fixed_match = re.fullmatch(
            r"(?:(?:前(?P<days>[0-9零一二两三四五六七八九十]+)天)|(?P<one_day>前一天)|(?P<same_day>当天))"
            r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
            r"(?P<hour>[0-9]{1,2})"
            r"(?:[点时:：](?P<minute>[0-9]{1,2}))?"
            r"(?:分)?(?:提醒|通知)?",
            compact,
        )
        if fixed_match:
            if fixed_match.group("same_day"):
                days_before = 0
            elif fixed_match.group("one_day"):
                days_before = 1
            else:
                days_before = self._parse_number_token(fixed_match.group("days"))
            return self._build_fixed_clock_rule(
                ddl_type=type_text,
                days_before=days_before,
                hour_text=fixed_match.group("hour"),
                minute_text=fixed_match.group("minute"),
                period_text=fixed_match.group("period"),
                rule_text=rule_part,
            )
        return None

    def _build_relative_rule(self, ddl_type: str, value_text: str, unit_text: str, rule_text: str) -> dict | None:
        """Build a relative reminder rule such as 提前1天提醒."""
        target = self._normalize_type_keyword(ddl_type)
        value = self._parse_number_token(value_text)
        if not target or value <= 0:
            return None

        unit_minutes = {
            "分钟": 1,
            "小时": 60,
            "天": 1440,
            "周": 10080,
            "礼拜": 10080,
        }.get(str(unit_text or "").strip(), 0)
        offset_minutes = value * unit_minutes
        if offset_minutes <= 0:
            return None

        return {
            "type_keyword": target,
            "match_text": target.lower(),
            "mode": "relative",
            "offset_minutes": offset_minutes,
            "rule_text": str(rule_text or "").strip() or f"提前{value_text}{unit_text}提醒",
            "updated_at": int(time()),
        }

    def _build_fixed_clock_rule(
        self,
        ddl_type: str,
        days_before: int,
        hour_text: str,
        minute_text: str | None,
        period_text: str | None,
        rule_text: str,
    ) -> dict | None:
        """Build a fixed-clock reminder rule such as 前一天晚上22点提醒."""
        target = self._normalize_type_keyword(ddl_type)
        hour = _safe_int(hour_text, default=-1)
        minute = _safe_int(minute_text, default=0, minimum=0)
        if not target or days_before < 0:
            return None

        hour = self._apply_period_to_hour(hour, str(period_text or "").strip())
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None

        return {
            "type_keyword": target,
            "match_text": target.lower(),
            "mode": "fixed_clock",
            "days_before": days_before,
            "hour": hour,
            "minute": minute,
            "period_text": str(period_text or "").strip(),
            "rule_text": str(rule_text or "").strip(),
            "updated_at": int(time()),
        }

    def _upsert_reminder_rule(self, group_state: dict, rule: dict) -> bool:
        """Insert or update a reminder rule for the current group."""
        rule_map = group_state.setdefault("reminder_rules", {})
        if not isinstance(rule_map, dict):
            group_state["reminder_rules"] = {}
            rule_map = group_state["reminder_rules"]

        key = str(rule.get("match_text") or self._normalize_type_keyword(rule.get("type_keyword"))).lower()
        if not key:
            return False
        existing = rule_map.get(key)
        if existing == rule:
            return False
        rule_map[key] = dict(rule)
        return True

    def _list_reminder_rules(self, group_state: dict) -> list[dict]:
        """List reminder rules sorted by keyword length descending."""
        rule_map = group_state.get("reminder_rules", {})
        if not isinstance(rule_map, dict):
            return []
        return sorted(
            [dict(rule) for rule in rule_map.values() if isinstance(rule, dict)],
            key=lambda item: len(str(item.get("match_text") or item.get("type_keyword") or "")),
            reverse=True,
        )

    def _find_matching_reminder_rule(self, group_state: dict, item: dict) -> dict | None:
        """Find the most specific reminder rule matching a ddl item."""
        haystacks = [
            self._normalize_match_text(item.get("type")),
            self._normalize_match_text(item.get("title")),
            self._normalize_match_text(item.get("source_text")),
        ]
        for rule in self._list_reminder_rules(group_state):
            match_text = str(rule.get("match_text") or "").strip().lower()
            if not match_text:
                continue
            for haystack in haystacks:
                if not haystack:
                    continue
                if haystack == match_text or match_text in haystack or haystack in match_text:
                    return rule
        return None

    def _get_item_remind_plan(self, group_state: dict, item: dict, deadline_ts: int | None = None) -> dict:
        """Return remind timestamp and rule metadata for a ddl item."""
        final_deadline_ts = deadline_ts or self._item_deadline_ts(item)
        if final_deadline_ts <= 0:
            return {"remind_ts": 0, "remind_key": "", "rule_text": ""}

        rule = self._find_matching_reminder_rule(group_state, item)
        if rule:
            remind_ts = self._compute_rule_remind_ts(final_deadline_ts, rule)
            rule_key = self._build_rule_key(rule)
            rule_text = str(rule.get("rule_text") or "").strip()
        else:
            remind_ts = final_deadline_ts - self._remind_before_minutes() * 60
            rule_key = f"default:{self._remind_before_minutes()}"
            rule_text = f"提前{self._remind_before_minutes()}分钟提醒"

        if remind_ts >= final_deadline_ts:
            remind_ts = max(0, final_deadline_ts - 60)
        return {
            "remind_ts": remind_ts,
            "remind_key": rule_key,
            "rule_text": rule_text,
        }

    def _compute_rule_remind_ts(self, deadline_ts: int, rule: dict) -> int:
        """Compute the actual remind timestamp for a rule and deadline."""
        mode = str(rule.get("mode") or "").strip()
        if mode == "relative":
            offset_minutes = _safe_int(rule.get("offset_minutes"), default=0, minimum=0)
            return max(0, deadline_ts - offset_minutes * 60)

        if mode == "fixed_clock":
            deadline_dt = datetime.fromtimestamp(deadline_ts)
            days_before = _safe_int(rule.get("days_before"), default=0, minimum=0)
            hour = _safe_int(rule.get("hour"), default=0, minimum=0)
            minute = _safe_int(rule.get("minute"), default=0, minimum=0)
            target_date = (deadline_dt - timedelta(days=days_before)).date()
            return int(
                datetime(
                    target_date.year,
                    target_date.month,
                    target_date.day,
                    hour,
                    minute,
                    0,
                ).timestamp()
            )
        return max(0, deadline_ts - self._remind_before_minutes() * 60)

    def _build_rule_key(self, rule: dict) -> str:
        """Build a stable rule key for deduplicating reminders."""
        mode = str(rule.get("mode") or "").strip()
        if mode == "relative":
            return f"relative:{str(rule.get('match_text') or '')}:{_safe_int(rule.get('offset_minutes'), default=0, minimum=0)}"
        if mode == "fixed_clock":
            return (
                f"fixed:{str(rule.get('match_text') or '')}:"
                f"{_safe_int(rule.get('days_before'), default=0, minimum=0)}:"
                f"{_safe_int(rule.get('hour'), default=0, minimum=0)}:"
                f"{_safe_int(rule.get('minute'), default=0, minimum=0)}"
            )
        return f"default:{self._remind_before_minutes()}"

    def _serialize_reminder_rule(self, rule: dict) -> dict:
        """Serialize a reminder rule for logging or llm-tool output."""
        payload = {
            "type_keyword": str(rule.get("type_keyword") or "").strip(),
            "mode": str(rule.get("mode") or "").strip(),
            "rule_text": str(rule.get("rule_text") or "").strip(),
        }
        if payload["mode"] == "relative":
            payload["offset_minutes"] = _safe_int(rule.get("offset_minutes"), default=0, minimum=0)
        elif payload["mode"] == "fixed_clock":
            payload["days_before"] = _safe_int(rule.get("days_before"), default=0, minimum=0)
            payload["hour"] = _safe_int(rule.get("hour"), default=0, minimum=0)
            payload["minute"] = _safe_int(rule.get("minute"), default=0, minimum=0)
        return payload

    def _get_pending_future_tasks(self, group_id: str, group_state: dict, limit: int) -> list[dict]:
        """Return pending FutureTask plans that the main agent should create."""
        if not self._auto_remind_enabled():
            return []

        now_ts = int(time())
        pending_items = []
        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts <= 0 or deadline_ts <= now_ts:
                continue

            remind_plan = self._get_item_remind_plan(group_state, item, deadline_ts)
            remind_ts = _safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
            remind_key = str(remind_plan.get("remind_key") or "")
            if remind_ts <= now_ts or not remind_key:
                continue

            expected_task_name = self._build_future_task_name(group_id, item, remind_key)
            recorded_task_name = str(item.get("future_task_name") or "").strip()
            recorded_key = str(item.get("future_task_remind_key") or "").strip()
            recorded_ts = _safe_int(item.get("future_task_remind_ts"), default=0, minimum=0)
            if (
                recorded_task_name == expected_task_name
                and recorded_key == remind_key
                and recorded_ts == remind_ts
            ):
                continue

            stale_task_names = []
            if recorded_task_name and recorded_task_name != expected_task_name:
                stale_task_names.append(recorded_task_name)

            pending_items.append(
                self._build_future_task_payload(
                    group_id=group_id,
                    group_state=group_state,
                    item=item,
                    deadline_ts=deadline_ts,
                    remind_plan=remind_plan,
                    task_name=expected_task_name,
                    stale_task_names=stale_task_names,
                )
            )

        pending_items.sort(key=lambda item: item.get("remind_ts", 0))
        return pending_items[:limit]

    def _build_future_task_payload(
        self,
        group_id: str,
        group_state: dict,
        item: dict,
        deadline_ts: int,
        remind_plan: dict,
        task_name: str,
        stale_task_names: list[str],
    ) -> dict:
        """Build one pending FutureTask creation payload for the main agent."""
        remind_ts = _safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
        remind_key = str(remind_plan.get("remind_key") or "")
        deadline_display = (
            str(item.get("normalized_deadline") or "").strip()
            or str(item.get("deadline_text") or "").strip()
            or _format_ts(deadline_ts)
        )
        rule_text = str(remind_plan.get("rule_text") or "").strip() or f"提前{self._remind_before_minutes()}分钟提醒"
        return {
            "fingerprint": str(item.get("fingerprint") or "").strip(),
            "type": str(item.get("type") or "").strip() or "其他",
            "title": str(item.get("title") or "").strip() or "未命名 DDL",
            "deadline_text": str(item.get("deadline_text") or "").strip(),
            "normalized_deadline": deadline_display,
            "deadline_ts": deadline_ts,
            "source_text": str(item.get("source_text") or "").strip(),
            "remind_key": remind_key,
            "rule_text": rule_text,
            "remind_ts": remind_ts,
            "remind_at": _format_ts(remind_ts),
            "task_name": task_name,
            "task_note": self._build_future_task_note(item, deadline_display),
            "stale_task_names": stale_task_names,
            "group_id": group_id,
            "unified_msg_origin": str(group_state.get("unified_msg_origin") or ""),
        }

    def _build_future_task_name(self, group_id: str, item: dict, remind_key: str) -> str:
        """Build a deterministic task name for the official FutureTask."""
        raw = "|".join(
            [
                str(group_id or "").strip(),
                str(item.get("fingerprint") or "").strip(),
                str(remind_key or "").strip(),
            ]
        )
        suffix = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"ddl_tracker_{group_id}_{suffix}"

    def _build_future_task_note(self, item: dict, deadline_display: str) -> str:
        """Build the note that will be executed by the official FutureTask main agent."""
        title = str(item.get("title") or "").strip() or "未命名 DDL"
        item_type = str(item.get("type") or "").strip() or "其他"
        source_text = str(item.get("source_text") or "").strip()
        return (
            "这是 ddl_tracker 插件生成的 DDL 提醒任务。"
            "请先确认该 DDL 仍然存在且尚未截止；如有需要，可调用 ddl_get_remaining 或 ddl_get_due_within 进行核对。"
            "确认后，直接在当前群里发送一条简短中文提醒，不要重新创建 future_task，不要闲聊。"
            f"提醒内容：{title}（{item_type}）将于 {deadline_display} 截止。"
            f"原始消息：{source_text}"
        )

    def _tool_item_payload(self, item: dict) -> dict:
        """Build a compact tool payload for a ddl item."""
        deadline_ts = self._item_deadline_ts(item)
        return {
            "fingerprint": str(item.get("fingerprint") or "").strip(),
            "type": str(item.get("type") or "").strip() or "其他",
            "title": str(item.get("title") or "").strip() or "未命名 DDL",
            "deadline_text": str(item.get("deadline_text") or "").strip(),
            "normalized_deadline": (
                str(item.get("normalized_deadline") or "").strip()
                or str(item.get("deadline_text") or "").strip()
                or _format_ts(deadline_ts)
            ),
            "source_text": str(item.get("source_text") or "").strip(),
            "remaining": self._format_remaining(deadline_ts - int(time())) if deadline_ts > 0 else "未知",
        }

    def _get_due_within_ddls(self, group_state: dict, hours: int) -> list[dict]:
        """Return ddl items due within the requested number of hours."""
        now_ts = int(time())
        limit_ts = now_ts + _safe_int(hours, default=24, minimum=1) * 3600
        result = []
        for item in self._get_nearest_ddls(group_state, limit=9999):
            if item["deadline_ts"] <= limit_ts:
                result.append(item)
        return result

    def _normalize_type_keyword(self, raw_text: str) -> str:
        """Normalize a rule target such as 作业 / 考试 / 讲座报告."""
        text = str(raw_text or "").strip()
        text = re.sub(r"(类|事项|任务|ddl)$", "", text, flags=re.IGNORECASE)
        return text.strip()

    def _normalize_match_text(self, raw_text: str) -> str:
        """Normalize text for keyword matching."""
        return re.sub(r"\s+", "", str(raw_text or "").strip()).lower()

    def _parse_number_token(self, raw_text: str) -> int:
        """Parse simple Arabic/Chinese number tokens used in reminder rules."""
        text = str(raw_text or "").strip()
        if not text:
            return 0
        if text == "半":
            return 0
        try:
            return int(text)
        except ValueError:
            pass

        mapping = {
            "零": 0,
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if text == "十":
            return 10
        if text.startswith("十"):
            return 10 + mapping.get(text[1:], 0)
        if text.endswith("十"):
            return mapping.get(text[0], 0) * 10
        if "十" in text:
            left, right = text.split("十", 1)
            return mapping.get(left, 0) * 10 + mapping.get(right, 0)
        return mapping.get(text, 0)

    def _apply_period_to_hour(self, hour: int, period_text: str) -> int:
        """Adjust hour based on Chinese time periods such as 晚上/下午."""
        if hour < 0:
            return hour
        period = str(period_text or "").strip()
        if period in {"凌晨"}:
            return 0 if hour == 12 else hour
        if period in {"早上", "上午"}:
            return 0 if hour == 12 else hour
        if period in {"中午"}:
            if hour == 12:
                return 12
            return hour + 12 if 1 <= hour <= 11 else hour
        if period in {"下午", "晚上"}:
            if 1 <= hour <= 11:
                return hour + 12
            return hour
        return hour

    def _ensure_group_state(self, group_id: str, event: AstrMessageEvent) -> dict:
        """Ensure minimal group state keys are present."""
        group_state = self.state.get(group_id, {})
        self._ensure_group_state_fields(group_state)
        group_state["unified_msg_origin"] = str(
            event.unified_msg_origin or group_state.get("unified_msg_origin") or ""
        )
        self.state[group_id] = group_state
        return group_state

    def _parse_minutes_arg(self, raw_message: str, default: int) -> int:
        """Parse optional command argument like /ddl_extract 60."""
        message = str(raw_message or "").strip()
        parts = message.split()
        if len(parts) >= 2:
            return _safe_int(parts[1], default=default, minimum=1)
        return _safe_int(default, default=default, minimum=1)

    def _parse_limit_arg(self, raw_message: str, default: int) -> int:
        """Parse optional command argument like /ddl_nearest 5."""
        return self._parse_minutes_arg(raw_message, default=default)

    def _is_plugin_command_text(self, raw_message: str) -> bool:
        """Whether a text message is one of the plugin commands."""
        message = str(raw_message or "").strip()
        if not message:
            return False
        return message.split()[0].lower() in COMMAND_NAMES

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """Read group id from the current event."""
        return str(event.get_group_id() or "").strip()

    async def _get_provider_id(self, unified_msg_origin: str) -> str:
        """Get configured provider id or current session provider id."""
        configured_provider_id = str(self.config.get("llm_provider_id", "") or "").strip()
        if configured_provider_id:
            return configured_provider_id

        if not unified_msg_origin:
            return ""

        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=unified_msg_origin
            )
        except TypeError:
            provider_id = await self.context.get_current_chat_provider_id(
                unified_msg_origin
            )
        except Exception as exc:
            logger.exception("[ddl_tracker] get provider id failed: %s", exc)
            return ""
        return str(provider_id or "").strip()

    def _build_ai_extract_prompt(self, messages: list[dict]) -> str:
        """Build AI extraction prompt from recent group messages."""
        lines = []
        for index, message in enumerate(messages, start=1):
            sender_name = message.get("sender_name") or message.get("sender_id") or ""
            message_ts = int(message.get("message_ts") or 0)
            timestamp = _format_ts(message_ts)
            lines.append(
                f"[{index}][ts={message_ts}][time={timestamp}][{sender_name}] "
                f"{message.get('message_text') or ''}"
            )

        example = {
            "summary": "群里提到了 3 条明确的 DDL。",
            "items": [
                {
                    "message_index": 1,
                    "type": "考试",
                    "title": "线代期中考试",
                    "deadline_text": "2026-04-26 10:00",
                    "normalized_deadline": "2026-04-26 10:00",
                    "source_text": "考试 2026-4-26-10:00",
                },
                {
                    "message_index": 2,
                    "type": "作业",
                    "title": "物理作业",
                    "deadline_text": "一小时后截止",
                    "normalized_deadline": "2026-04-25 23:30",
                    "source_text": "一小时后物理作业截止",
                },
                {
                    "message_index": 3,
                    "type": "作业",
                    "title": "实验报告",
                    "deadline_text": "一周内提交",
                    "normalized_deadline": "2026-05-02 21:30",
                    "source_text": "一周内提交实验报告",
                },
            ],
        }

        return "\n".join(
            [
                self._extract_prompt(),
                f"当前时间：{_format_ts(int(time()))}",
                "示例：",
                json.dumps(example, ensure_ascii=False, indent=2),
                "待提取消息：",
                "\n".join(lines),
            ]
        )

    def _extract_prompt(self) -> str:
        """Return configurable extraction prompt with default fallback."""
        prompt = str(self.config.get("extract_prompt", "") or "").strip()
        return prompt or DEFAULT_EXTRACT_PROMPT

    def _get_nearest_ddls(self, group_state: dict, limit: int) -> list[dict]:
        """Return upcoming ddl items with parseable deadlines, sorted ascending."""
        now_ts = int(time())
        sortable_items = []
        for item in group_state.get("ddl_items", []):
            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts <= 0 or deadline_ts < now_ts:
                continue
            sortable_items.append(
                {
                    "title": str(item.get("title") or "").strip() or "未命名 DDL",
                    "type": str(item.get("type") or "").strip() or "其他",
                    "deadline_text": str(item.get("deadline_text") or "").strip(),
                    "normalized_deadline": (
                        str(item.get("normalized_deadline") or "").strip()
                        or str(item.get("deadline_text") or "").strip()
                        or _format_ts(deadline_ts)
                    ),
                    "source_text": str(item.get("source_text") or "").strip(),
                    "deadline_ts": deadline_ts,
                }
            )

        sortable_items.sort(key=lambda item: item["deadline_ts"])
        return sortable_items[:limit]

    def _item_deadline_ts(self, item: dict) -> int:
        """Get a sortable deadline timestamp from a stored ddl item."""
        stored_deadline_ts = _safe_int(item.get("deadline_ts"), default=0, minimum=0)
        if stored_deadline_ts > 0:
            return stored_deadline_ts

        for key in ("normalized_deadline", "deadline_text"):
            deadline_ts = self._parse_deadline_ts(str(item.get(key) or "").strip())
            if deadline_ts > 0:
                item["deadline_ts"] = deadline_ts
                return deadline_ts
        return 0

    def _parse_deadline_ts(self, deadline_text: str) -> int:
        """Parse normalized deadline text into unix timestamp."""
        raw = str(deadline_text or "").strip().replace("/", "-").replace("T", " ")
        if not raw:
            return 0

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H",
            "%Y-%m-%d-%H:%M:%S",
            "%Y-%m-%d-%H:%M",
            "%Y-%m-%d-%H",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt == "%Y-%m-%d":
                    parsed = parsed.replace(hour=23, minute=59, second=59)
                return int(parsed.timestamp())
            except ValueError:
                continue
        return 0

    def _format_remaining(self, remaining_seconds: int) -> str:
        """Format remaining time into a short Chinese string."""
        seconds = _safe_int(remaining_seconds, default=0)
        if seconds <= 0:
            return "已截止"

        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60

        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0:
            parts.append(f"{minutes}分钟")
        if not parts:
            parts.append("不足1分钟")
        return "".join(parts[:2])

    def _auto_extract_enabled(self) -> bool:
        """Whether auto extraction is enabled."""
        return bool(self.config.get("auto_extract_enabled", True))

    def _auto_remind_enabled(self) -> bool:
        """Whether auto reminder is enabled."""
        return bool(self.config.get("auto_remind_enabled", True))

    def _remind_before_minutes(self) -> int:
        """How many minutes before deadline the bot should remind once."""
        return _safe_int(
            self.config.get("remind_before_minutes", DEFAULT_REMIND_BEFORE_MINUTES),
            default=DEFAULT_REMIND_BEFORE_MINUTES,
            minimum=1,
        )

    def _auto_extract_interval_minutes(self) -> int:
        """Auto/manual default extraction window in minutes."""
        return _safe_int(
            self.config.get(
                "auto_extract_interval_minutes",
                DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES,
            ),
            default=DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES,
            minimum=1,
        )

    def _dump_config(self) -> dict:
        """Return a small normalized config snapshot for debugging."""
        return {
            "llm_provider_id": str(self.config.get("llm_provider_id", "") or "").strip(),
            "auto_extract_enabled": self._auto_extract_enabled(),
            "auto_extract_interval_minutes": self._auto_extract_interval_minutes(),
            "auto_remind_enabled": self._auto_remind_enabled(),
            "remind_before_minutes": self._remind_before_minutes(),
            "extract_prompt": self._extract_prompt(),
        }

    def _persist(self):
        """Persist in-memory state to disk."""
        _save_state(self.state)
