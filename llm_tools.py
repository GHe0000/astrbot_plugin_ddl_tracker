import json

from astrbot.api.event import AstrMessageEvent

from .utils import safe_int


class LLMToolsMixin:
    async def ddl_extract_recent_messages_tool(self, event: AstrMessageEvent, minutes: int = 0):
        '''整理最近一段时间的群消息并提取 DDL。

        如果返回 pending_future_task_count > 0，主 Agent 必须继续调用官方 future_task
        创建每一条 pending_future_tasks，并在创建成功后调用 ddl_mark_future_task_created。

        Args:
            minutes(number): 要整理最近多少分钟的消息；填 0 时使用插件默认自动整理周期。
        '''
        group_id = self._get_group_id(event)
        if not group_id:
            return json.dumps({"ok": False, "reason": "该工具只能在群聊中使用"}, ensure_ascii=False)

        group_state = self._prepare_group_state_for_read(group_id)
        if not bool(group_state.get("enabled", False)):
            return json.dumps({"ok": False, "reason": "当前群尚未开启 DDL"}, ensure_ascii=False)

        requested_minutes = safe_int(minutes, default=0, minimum=0)
        lookback_minutes = requested_minutes or self._auto_extract_interval_minutes()
        result = await self._extract_group_ddls(
            group_id=group_id,
            group_state=group_state,
            unified_msg_origin=str(event.unified_msg_origin or group_state.get("unified_msg_origin") or ""),
            lookback_minutes=lookback_minutes,
            source="tool",
        )
        group_state = self.state.get(group_id, group_state)
        pending_future_tasks = self._get_pending_future_tasks(
            group_id=group_id,
            group_state=group_state,
            limit=20,
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
            "future_task_sync_result": result.get("future_task_sync_result", {}),
            "pending_future_task_count": len(pending_future_tasks),
            "pending_future_tasks": pending_future_tasks,
            "future_task_sync": self._build_future_task_sync_instruction(pending_future_tasks),
        }
        return json.dumps(payload, ensure_ascii=False)

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

    async def ddl_set_type_reminder_tool(self, event: AstrMessageEvent, ddl_type: str, rule_text: str):
        '''为某一类 DDL 设置提醒规则。

        如果返回 pending_future_task_count > 0，主 Agent 必须继续调用官方 future_task
        创建每一条 pending_future_tasks，并在创建成功后调用 ddl_mark_future_task_created。

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
        sync_result = await self._sync_pending_future_tasks(
            group_id=group_id,
            group_state=group_state,
            source="tool_rule",
        )
        pending_future_tasks = self._get_pending_future_tasks(
            group_id=group_id,
            group_state=group_state,
            limit=20,
        )
        payload = {
            "ok": True,
            "group_id": group_id,
            "rule": self._serialize_reminder_rule(rule),
            "future_task_sync_result": sync_result,
            "pending_future_task_count": len(pending_future_tasks),
            "pending_future_tasks": pending_future_tasks,
            "future_task_sync": self._build_future_task_sync_instruction(pending_future_tasks),
        }
        return json.dumps(payload, ensure_ascii=False)

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

    async def ddl_get_pending_future_tasks_tool(self, event: AstrMessageEvent, limit: int = 20):
        '''获取当前群需要由主 Agent 创建的官方 FutureTask 提醒计划。

        如果返回 count > 0，主 Agent 必须继续调用官方 future_task 创建每一条 items，
        并在创建成功后调用 ddl_mark_future_task_created。

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
            "pending_future_tasks": items,
            "future_task_sync": self._build_future_task_sync_instruction(items),
        }
        return json.dumps(payload, ensure_ascii=False)

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
        actual_task_name = str(task_name or "").strip()
        if not fingerprint_text or not remind_key_text or not actual_task_name:
            return json.dumps(
                {"ok": False, "reason": "fingerprint、remind_key、task_name 不能为空"},
                ensure_ascii=False,
            )

        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("fingerprint") or "").strip() != fingerprint_text:
                continue

            record_result = self._record_future_task_created(
                group_state=group_state,
                group_id=group_id,
                fingerprint=fingerprint_text,
                remind_key=remind_key_text,
                actual_task_name=actual_task_name,
            )
            if not record_result.get("ok"):
                payload = {
                    "ok": False,
                    "group_id": group_id,
                    "fingerprint": fingerprint_text,
                    **record_result,
                }
                return json.dumps(payload, ensure_ascii=False)

            self.state[group_id] = group_state
            self._persist()
            payload = {
                "ok": True,
                "group_id": group_id,
                "fingerprint": fingerprint_text,
                **record_result,
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
