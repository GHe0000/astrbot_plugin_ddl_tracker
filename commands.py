import json
from time import time

from astrbot.api.event import AstrMessageEvent, filter

from constants import MAX_MESSAGES_PER_GROUP
from utils import safe_int, format_ts, format_remaining


class CommandsMixin:
    @filter.command("ddl_on")
    async def ddl_on(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._ensure_group_state(group_id, event)
        group_state["enabled"] = True
        self._persist()
        from astrbot.api import logger

        logger.info("[ddl_tracker] ddl_on group=%s", group_id)
        yield event.plain_result(f"已开启 DDL，group_id={group_id}")

    @filter.command("ddl_off")
    async def ddl_off(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return

        group_state = self._ensure_group_state(group_id, event)
        group_state["enabled"] = False
        self._persist()
        from astrbot.api import logger

        logger.info("[ddl_tracker] ddl_off group=%s", group_id)
        yield event.plain_result(f"已关闭 DDL，group_id={group_id}")

    @filter.command("ddl_status")
    async def ddl_status(self, event: AstrMessageEvent):
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
        last_extract_at = format_ts(group_state.get("last_extract_at", 0))
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
        async for result in self._handle_extract_command(event, command_name="/ddl_extract"):
            yield result

    @filter.command("ddl_nearest")
    async def ddl_nearest(self, event: AstrMessageEvent):
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
            remain_text = format_remaining(item["deadline_ts"] - now_ts)
            lines.append(
                f"{index}. {item['title']} | {item['type']} | "
                f"截止={item['normalized_deadline']} | 剩余={remain_text}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
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
            from astrbot.api import logger

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

    async def _handle_extract_command(self, event: AstrMessageEvent, command_name: str):
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
        from astrbot.api import logger

        logger.info(
            "[ddl_tracker] manual extract payload=%s",
            json.dumps(payload, ensure_ascii=False),
        )
        yield event.plain_result(
            f"手动整理完成：提取 {payload['extracted_count']} 条，"
            f"新增 {payload['added_count']} 条，更新 {payload['updated_count']} 条。"
        )

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or "").strip()

    def _parse_minutes_arg(self, raw_message: str, default: int) -> int:
        message = str(raw_message or "").strip()
        parts = message.split()
        if len(parts) >= 2:
            return safe_int(parts[1], default=default, minimum=1)
        return safe_int(default, default=default, minimum=1)

    def _parse_limit_arg(self, raw_message: str, default: int) -> int:
        return self._parse_minutes_arg(raw_message, default=default)
