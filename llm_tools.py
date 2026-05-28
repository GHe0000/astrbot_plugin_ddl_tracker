import json
from time import time

from astrbot.api.event import AstrMessageEvent, filter

from utils import safe_int, format_ts


class LLMToolsMixin:
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

        lookback_minutes = safe_int(minutes, default=self._auto_extract_interval_minutes(), minimum=1)
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
        items = self._get_nearest_ddls(group_state, safe_int(limit, default=10, minimum=1))
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
        items = self._get_due_within_ddls(group_state, safe_int(hours, default=24, minimum=1))
        payload = {
            "ok": True,
            "group_id": group_id,
            "hours": safe_int(hours, default=24, minimum=1),
            "count": len(items),
            "items": [self._tool_item_payload(item) for item in items],
        }
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="ddl_set_type_reminder")
    async def ddl_set_type_reminder_tool(self, event: AstrMessageEvent, ddl_type: str, rule_text: str):
        '''为某一类 DDL 设置提醒规则。

        Args:
            ddl_type(string): DDL 类型关键词，例如作业、考试、讲座报告。
            rule_text(string): 提醒规则文本，例如"提前1天提醒""前一天晚上22点提醒"。
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
            limit=safe_int(limit, default=20, minimum=1),
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
            current_remind_ts = safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
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
                "remind_at": format_ts(current_remind_ts),
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
