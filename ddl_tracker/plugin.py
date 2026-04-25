"""AstrBot 插件入口，负责命令、事件和后台流程编排。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .data_ops import (
    build_conversation_text,
    build_rule_from_input,
    parse_extract_result,
    rule_to_text,
    serialize_deadline,
    serialize_rule,
)
from .reminder_loop import ReminderLoop, apply_rule_to_existing_deadlines, save_extract_result
from .settings import PluginSettings
from .sql_store import DDLStore
from .utils import safe_int, safe_json_loads

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    get_astrbot_data_path = None


@register(
    "ddl_tracker",
    "Codex",
    "群聊 DDL 记录、提取与提醒插件",
    "1.2.0",
)
class DDLTrackerPlugin(Star):
    """DDL Tracker 的 AstrBot 插件实现。"""

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        """初始化配置、数据库和后台循环依赖。"""
        super().__init__(context)
        self.settings = PluginSettings(config)
        self.data_dir = self._get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.store = DDLStore(self.data_dir / "ddl_tracker.sqlite3")
        self.store.initialize()
        self.reminder_loop = ReminderLoop(
            store=self.store,
            settings=self.settings,
            send_text=self._send_text_to_origin,
        )

        self._extracting_groups: set[str] = set()
        self._bg_task: asyncio.Task | None = None
        self._stopped = False

        try:
            self.context.add_llm_tools()
        except Exception as exc:
            logger.warning(f"[ddl_tracker] add_llm_tools failed: {exc}")

    async def initialize(self):
        """在插件加载后启动后台循环。"""
        self._stopped = False
        self._bg_task = asyncio.create_task(self._background_loop())
        logger.info("[ddl_tracker] background loop started")

    async def terminate(self):
        """在插件卸载前停止后台循环。"""
        self._stopped = True
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[ddl_tracker] background loop stop error: {exc}")
        logger.info("[ddl_tracker] background loop stopped")

    @filter.command("ddl_on")
    async def ddl_on(self, event: AstrMessageEvent):
        """管理员在当前群开启 DDL 跟踪。"""
        if not self._can_manage_group(event):
            yield event.plain_result("只有群管理员或配置中的管理员账号可以开启该功能。")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        self.store.enable_group(
            group_id=group_id,
            unified_msg_origin=self._get_unified_msg_origin(event),
            now_ts=self.settings.now_ts(),
        )
        yield event.plain_result(
            "当前群已开启 DDL 跟踪。\n"
            "接下来会自动记录纯文本消息、周期抽取 DDL，并在到达提醒时间时自动提醒。"
        )

    @filter.command("ddl_off")
    async def ddl_off(self, event: AstrMessageEvent):
        """管理员在当前群关闭 DDL 跟踪。"""
        if not self._can_manage_group(event):
            yield event.plain_result("只有群管理员或配置中的管理员账号可以关闭该功能。")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        self.store.disable_group(group_id=group_id, now_ts=self.settings.now_ts())
        yield event.plain_result("当前群已关闭 DDL 跟踪。")

    @filter.command("ddl_status")
    async def ddl_status(self, event: AstrMessageEvent):
        """查看当前群的 DDL 跟踪状态。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group = self.store.get_group(group_id)
        if not group:
            yield event.plain_result("当前群还没有开启 DDL 跟踪。")
            return

        message_count = self.store.count_messages(group_id)
        pending_count = self.store.count_pending_deadlines(group_id)
        rule_count = self.store.count_rules(group_id)
        next_deadline = self.store.get_next_pending_deadline(group_id)

        enabled_text = "开启" if group["enabled"] else "关闭"
        lines = [
            f"group_id: {group_id}",
            f"status: {enabled_text}",
            f"recorded_messages: {message_count}",
            f"pending_deadlines: {pending_count}",
            f"category_rules: {rule_count}",
            f"last_extract_at: {self.settings.format_ts(group['last_extract_at'])}",
            f"last_message_row_id: {group['last_message_row_id']}",
            f"updated_at: {self.settings.format_ts(group['updated_at'])}",
        ]
        if next_deadline:
            lines.append(
                "next_deadline: "
                f"{next_deadline['description']} | {next_deadline['category']} | "
                f"{self.settings.format_ts(next_deadline['deadline_ts'])}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("ddl_now")
    async def ddl_now(self, event: AstrMessageEvent):
        """立即抽取当前群最近尚未处理的消息。"""
        if not self._can_manage_group(event):
            yield event.plain_result("只有群管理员或配置中的管理员账号可以手动触发抽取。")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        report = await self._run_group_extract(group_id=group_id, force=True)
        yield event.plain_result(report["message"])

    @filter.command("ddl_list")
    async def ddl_list(self, event: AstrMessageEvent):
        """列出当前群待处理的 DDL。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        self.store.mark_overdue_deadlines(self.settings.now_ts())
        rows = self.store.list_pending_deadlines(group_id=group_id, limit=20)
        payload = {
            "group_id": group_id,
            "deadlines": [serialize_deadline(row, self.settings.format_ts) for row in rows],
        }
        if not payload["deadlines"]:
            yield event.plain_result("当前群没有剩余 DDL。")
            return
        yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))

    @filter.command("ddl_due")
    async def ddl_due(self, event: AstrMessageEvent):
        """列出当前群在指定小时内截止的 DDL。用法：/ddl_due 24"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        text = self._get_message_text(event)
        hours = 24
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            hours = safe_int(parts[1], default=24, minimum=1)

        self.store.mark_overdue_deadlines(self.settings.now_ts())
        rows = self.store.list_pending_deadlines_due_within(
            group_id=group_id,
            end_ts=self.settings.now_ts() + hours * 3600,
            limit=20,
        )
        payload = {
            "group_id": group_id,
            "hours": hours,
            "deadlines": [serialize_deadline(row, self.settings.format_ts) for row in rows],
        }
        if not payload["deadlines"]:
            yield event.plain_result(f"当前群在 {hours} 小时内没有即将截止的 DDL。")
            return
        yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))

    @filter.command("ddl_rules")
    async def ddl_rules(self, event: AstrMessageEvent):
        """查看当前群已生效的分类提醒规则。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        lines = [rule_to_text(rule) for rule in self.store.list_group_rules(group_id)]
        if not lines:
            yield event.plain_result("当前群还没有分类提醒规则。")
            return
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="ddl_extract_messages")
    async def ddl_extract_messages(self, event: AstrMessageEvent, text: str):
        """提取并整理一段文本中的 DDL 与分类提醒规则。

        Args:
            text(string): 需要分析的纯文本消息片段
        """
        result = await self._extract_structured_items_by_llm(
            unified_msg_origin=self._get_unified_msg_origin(event),
            group_id=self._get_group_id(event) or "adhoc",
            raw_text=text,
        )
        yield event.plain_result(json.dumps(result, ensure_ascii=False, indent=2))

    @filter.llm_tool(name="ddl_sync_group_deadlines")
    async def ddl_sync_group_deadlines(self, event: AstrMessageEvent):
        """手动同步当前群尚未处理的消息，提取最新 DDL。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该工具只能在群聊里使用。")
            return
        report = await self._run_group_extract(group_id=group_id, force=True)
        yield event.plain_result(report["message"])

    @filter.llm_tool(name="ddl_list_remaining_deadlines")
    async def ddl_list_remaining_deadlines(self, event: AstrMessageEvent, limit: int = 20):
        """获取当前群剩余的 DDL 列表。

        Args:
            limit(int): 最多返回多少条，默认 20
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result('{"deadlines":[],"error":"group_only"}')
            return
        self.store.mark_overdue_deadlines(self.settings.now_ts())
        rows = self.store.list_pending_deadlines(group_id=group_id, limit=safe_int(limit, default=20, minimum=1))
        payload = {
            "group_id": group_id,
            "deadlines": [serialize_deadline(row, self.settings.format_ts) for row in rows],
        }
        yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))

    @filter.llm_tool(name="ddl_list_due_within")
    async def ddl_list_due_within(self, event: AstrMessageEvent, hours: int = 24, limit: int = 20):
        """获取当前群在指定时间范围内截止的 DDL。

        Args:
            hours(int): 截止窗口，单位小时，默认 24
            limit(int): 最多返回多少条，默认 20
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result('{"deadlines":[],"error":"group_only"}')
            return
        self.store.mark_overdue_deadlines(self.settings.now_ts())
        rows = self.store.list_pending_deadlines_due_within(
            group_id=group_id,
            end_ts=self.settings.now_ts() + safe_int(hours, default=24, minimum=1) * 3600,
            limit=safe_int(limit, default=20, minimum=1),
        )
        payload = {
            "group_id": group_id,
            "hours": safe_int(hours, default=24, minimum=1),
            "deadlines": [serialize_deadline(row, self.settings.format_ts) for row in rows],
        }
        yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))

    @filter.llm_tool(name="ddl_set_category_reminder")
    async def ddl_set_category_reminder(
        self,
        event: AstrMessageEvent,
        category: str,
        remind_type: str,
        offset_minutes: int = 0,
        days_before: int = 1,
        fixed_hour: int = -1,
        fixed_minute: int = -1,
    ):
        """为某一类 DDL 设置提醒规则。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result('{"ok":false,"error":"group_only"}')
            return

        rule = build_rule_from_input(
            category=category,
            remind_type=remind_type,
            offset_minutes=offset_minutes,
            days_before=days_before,
            fixed_hour=fixed_hour,
            fixed_minute=fixed_minute,
            source_text="llm_tool",
            source_message_ids=[],
            created_by_sender_id=self._get_sender_id(event),
        )
        if not rule:
            yield event.plain_result('{"ok":false,"error":"invalid_rule"}')
            return

        self.store.upsert_reminder_rule(group_id=group_id, rule=rule, now_ts=self.settings.now_ts())
        updated_count = apply_rule_to_existing_deadlines(
            store=self.store,
            settings=self.settings,
            group_id=group_id,
            category=rule["category"],
        )
        payload = {
            "ok": True,
            "group_id": group_id,
            "category": rule["category"],
            "updated_deadline_count": updated_count,
            "rule": serialize_rule(rule),
        }
        yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群内纯文本消息，不依赖唤醒和 @。"""
        record = self._build_message_record(event)
        if not record:
            return
        if not self.store.group_enabled(record["group_id"]):
            return
        self.store.insert_message(record)

    async def _background_loop(self):
        """持续执行后台抽取和提醒任务。"""
        while not self._stopped:
            try:
                await self.reminder_loop.tick(self._run_group_extract)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"[ddl_tracker] background loop error: {exc}")
            await asyncio.sleep(self.settings.tick_interval_seconds())

    async def _run_group_extract(self, group_id: str, force: bool) -> dict:
        """执行单个群的新消息抽取流程。"""
        if group_id in self._extracting_groups:
            return {
                "group_id": group_id,
                "success": False,
                "message": f"群 {group_id} 正在执行抽取，请稍后再试。",
            }

        self._extracting_groups.add(group_id)
        try:
            now_ts = self.settings.now_ts()
            group = self.store.get_group(group_id)
            if not group:
                return {
                    "group_id": group_id,
                    "success": False,
                    "message": f"群 {group_id} 尚未开启 DDL 跟踪。",
                }
            if not group["enabled"] and not force:
                return {
                    "group_id": group_id,
                    "unified_msg_origin": group["unified_msg_origin"],
                    "success": False,
                    "message": f"群 {group_id} 当前未开启 DDL 跟踪。",
                }

            messages = self.store.fetch_extract_batch(
                group_id=group_id,
                last_message_row_id=group["last_message_row_id"],
                limit=self.settings.max_messages_per_extract(),
            )
            if not messages:
                self.store.touch_group_extract(group_id=group_id, now_ts=now_ts)
                return {
                    "group_id": group_id,
                    "unified_msg_origin": group["unified_msg_origin"],
                    "summary": "",
                    "success": True,
                    "processed_message_count": 0,
                    "message": f"群 {group_id} 暂无新的普通文本消息需要抽取。",
                }

            raw_text = build_conversation_text(messages, self.settings.format_ts)
            result = await self._extract_structured_items_by_llm(
                unified_msg_origin=group["unified_msg_origin"],
                group_id=group_id,
                raw_text=raw_text,
            )
            saved = save_extract_result(
                store=self.store,
                settings=self.settings,
                group_id=group_id,
                messages=messages,
                result=result,
            )
            last_row_id = messages[-1]["id"]
            self.store.touch_group_extract(
                group_id=group_id,
                now_ts=now_ts,
                last_message_row_id=last_row_id,
            )

            return {
                "group_id": group_id,
                "unified_msg_origin": group["unified_msg_origin"],
                "summary": result.get("summary", ""),
                "inserted_deadlines": saved["inserted_deadlines"],
                "upserted_rules": saved["upserted_rules"],
                "recalculated_deadlines": saved["recalculated_deadlines"],
                "processed_message_count": len(messages),
                "last_message_row_id": last_row_id,
                "success": True,
                "message": (
                    f"群 {group_id} 抽取完成：新增 DDL {saved['inserted_deadlines']} 条，"
                    f"更新提醒规则 {saved['upserted_rules']} 条，"
                    f"重算历史 DDL {saved['recalculated_deadlines']} 条。"
                ),
            }
        except Exception as exc:
            logger.exception(f"[ddl_tracker] extract failed group={group_id}: {exc}")
            return {
                "group_id": group_id,
                "success": False,
                "message": f"群 {group_id} 抽取失败：{exc}",
            }
        finally:
            self._extracting_groups.discard(group_id)

    async def _extract_structured_items_by_llm(
        self,
        unified_msg_origin: str,
        group_id: str,
        raw_text: str,
    ) -> dict:
        """调用模型提取摘要、DDL 和提醒规则。"""
        provider_id = await self._resolve_provider_id(unified_msg_origin)
        prompt = f"""
你是一个群聊 DDL 提取器。请根据下面的群聊文本完成两件事：

1. 用一小段中文总结这批消息里和待办/截止时间有关的信息。
2. 抽取所有明确的 DDL，以及所有“某一类 DDL 应该怎么提醒”的偏好规则。

当前时间戳：{self.settings.now_ts()}
当前时区：{self.settings.timezone_name()}
当前群 ID：{group_id}

输出必须是 JSON，结构如下：
{{
  "summary": "一句到几句中文摘要",
  "deadlines": [
    {{
      "description": "待办事项，简短明确",
      "category": "类别，尽量规范成短中文，例如 作业/考试/讲座报告/实验/课程设计/活动/报名/其他",
      "deadline_text": "原文里的时间表达",
      "deadline_ts": 1719999999,
      "confidence": 0.95,
      "source_text": "触发该 DDL 的原始语句",
      "source_message_ids": [1, 2]
    }}
  ],
  "reminder_rules": [
    {{
      "category": "类别，必须和 deadlines 中的 category 风格一致",
      "remind_type": "offset 或 fixed_day_before_time",
      "offset_minutes": 1440,
      "days_before": 1,
      "fixed_hour": 22,
      "fixed_minute": 0,
      "source_text": "触发该提醒规则的原始语句",
      "source_message_ids": [3]
    }}
  ]
}}

规则要求：
- 只输出 JSON，不要输出其他解释。
- 只提取高置信度、语义明确的 DDL。
- deadline_ts 必须是 Unix 秒级时间戳。
- 如果没有 DDL，deadlines 返回 []。
- 如果没有提醒规则，reminder_rules 返回 []。
- 如果用户表达的是“某类 DDL 提前 N 分钟/小时/天提醒”，使用 remind_type=offset，并换算成 offset_minutes。
- 如果用户表达的是“某类 DDL 在截止前 X 天的 HH:MM 提醒”，使用 remind_type=fixed_day_before_time。
- 没有把握时不要臆造。

群聊文本如下：
{raw_text}
""".strip()

        llm_resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        text = (getattr(llm_resp, "completion_text", "") or "").strip()
        payload = safe_json_loads(
            text,
            default={"summary": "", "deadlines": [], "reminder_rules": []},
        )
        return parse_extract_result(payload)

    async def _resolve_provider_id(self, unified_msg_origin: str) -> str:
        """解析当前群应使用的模型 Provider ID。"""
        configured_provider = self.settings.llm_provider_id()
        if configured_provider:
            return configured_provider

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
        raise RuntimeError("无法推断 llm_provider_id，请在插件配置页中显式填写。")

    def _build_message_record(self, event: AstrMessageEvent) -> dict | None:
        """把群消息事件整理成待入库记录。"""
        group_id = self._get_group_id(event)
        message_text = self._get_message_text(event)
        if not group_id or not message_text:
            return None

        now_ts = self.settings.now_ts()
        sender_id = self._get_sender_id(event)
        return {
            "group_id": group_id,
            "unified_msg_origin": self._get_unified_msg_origin(event),
            "sender_id": sender_id,
            "sender_name": self._get_sender_name(event),
            "sender_role": self._get_sender_role(event),
            "message_text": message_text,
            "message_ts": self._get_event_ts(event, now_ts),
            "is_command": message_text.startswith("/"),
            "is_bot": self._is_bot_sender(event, sender_id),
            "created_at": now_ts,
        }

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """从事件中读取群 ID。"""
        try:
            return str(event.message_obj.group_id or "")
        except Exception:
            return str(getattr(event, "group_id", "") or "")

    def _get_unified_msg_origin(self, event: AstrMessageEvent) -> str:
        """从事件中读取统一消息来源。"""
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        """从事件中读取发送者 ID。"""
        try:
            sender = event.message_obj.sender
            return str(getattr(sender, "user_id", "") or getattr(sender, "id", "") or "")
        except Exception:
            return ""

    def _get_sender_name(self, event: AstrMessageEvent) -> str:
        """从事件中读取发送者昵称。"""
        try:
            return str(event.get_sender_name() or "")
        except Exception:
            pass
        try:
            sender = event.message_obj.sender
            return str(getattr(sender, "nickname", "") or getattr(sender, "name", "") or "")
        except Exception:
            return ""

    def _get_sender_role(self, event: AstrMessageEvent) -> str:
        """从事件中读取发送者角色。"""
        for attr in ("role", "sender_role", "permission_type"):
            value = getattr(event, attr, None)
            if value:
                return str(value).lower()
        try:
            sender = event.message_obj.sender
            role = getattr(sender, "role", None)
            if role:
                return str(role).lower()
        except Exception:
            pass
        return ""

    def _get_message_text(self, event: AstrMessageEvent) -> str:
        """从事件中读取纯文本消息内容。"""
        text = str(getattr(event, "message_str", "") or "").strip()
        if text:
            return text
        try:
            return str(event.message_obj.message_str or "").strip()
        except Exception:
            return ""

    def _get_event_ts(self, event: AstrMessageEvent, now_ts: int) -> int:
        """从事件中读取消息时间戳。"""
        try:
            ts = int(getattr(event.message_obj, "timestamp", 0) or 0)
            if ts > 0:
                return ts
        except Exception:
            pass
        return now_ts

    def _is_bot_sender(self, event: AstrMessageEvent, sender_id: str) -> bool:
        """判断消息是否由机器人自己发送。"""
        try:
            self_id = str(getattr(event.message_obj, "self_id", "") or "")
            if self_id and self_id == str(sender_id):
                return True
        except Exception:
            pass
        return False

    def _can_manage_group(self, event: AstrMessageEvent) -> bool:
        """判断当前发送者是否有群管理权限。"""
        sender_id = self._get_sender_id(event)
        if sender_id and sender_id in self.settings.admin_ids():
            return True

        role = self._get_sender_role(event)
        return role in {"admin", "owner", "administrator", "group_admin", "group_owner"}

    async def _send_text_to_origin(self, unified_msg_origin: str, text: str):
        """向原群或原会话主动发送文本消息。"""
        if not unified_msg_origin:
            raise RuntimeError("unified_msg_origin 为空，无法主动发送消息。")

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

        raise RuntimeError("发送消息失败，请根据 AstrBot 当前版本调整消息链构造方式。")

    def _get_data_dir(self) -> Path:
        """返回插件的数据目录。"""
        if get_astrbot_data_path:
            return Path(get_astrbot_data_path()) / "plugin_data" / self.settings.plugin_name()
        return Path("data") / "plugin_data" / self.settings.plugin_name()
