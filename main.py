from __future__ import annotations

import asyncio
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .config import ConfigMixin
from .ddl_item import DDLItemMixin
from .extraction import ExtractionMixin
from .future_task import FutureTaskMixin
from .llm_tools import LLMToolsMixin
from .reminder_rules import ReminderRulesMixin
from .state_store import StateMixin
from .commands import CommandsMixin


@register(
    "ddl_tracker",
    "Guotao He",
    "DDL Tracker",
    "0.8.5",
)
class DDLTrackerPlugin(
    ConfigMixin,
    StateMixin,
    DDLItemMixin,
    ReminderRulesMixin,
    FutureTaskMixin,
    ExtractionMixin,
    CommandsMixin,
    LLMToolsMixin,
    Star,
):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(context)
        self.config = config or {}
        self.state = self._load_state()
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

    async def terminate(self):
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

    @filter.command("ddl_on")
    async def ddl_on(self, event: AstrMessageEvent):
        async for result in CommandsMixin.ddl_on(self, event):
            yield result

    @filter.command("ddl_off")
    async def ddl_off(self, event: AstrMessageEvent):
        async for result in CommandsMixin.ddl_off(self, event):
            yield result

    @filter.command("ddl_status")
    async def ddl_status(self, event: AstrMessageEvent):
        async for result in CommandsMixin.ddl_status(self, event):
            yield result

    @filter.command("ddl_extract")
    async def ddl_extract(self, event: AstrMessageEvent):
        async for result in CommandsMixin.ddl_extract(self, event):
            yield result

    @filter.command("ddl_nearest")
    async def ddl_nearest(self, event: AstrMessageEvent):
        async for result in CommandsMixin.ddl_nearest(self, event):
            yield result

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        await CommandsMixin.on_group_message(self, event)

    @filter.llm_tool(name="ddl_extract_recent_messages")
    async def ddl_extract_recent_messages_tool(self, event: AstrMessageEvent, minutes: int = 0):
        '''整理最近一段时间的群消息并提取 DDL。

        如果返回 pending_future_task_count > 0，主 Agent 必须继续调用官方 future_task
        创建每一条 pending_future_tasks，并在创建成功后调用 ddl_mark_future_task_created。

        Args:
            minutes(number): 要整理最近多少分钟的消息；填 0 时使用插件默认自动整理周期。
        '''
        return await LLMToolsMixin.ddl_extract_recent_messages_tool(self, event, minutes)

    @filter.llm_tool(name="ddl_get_remaining")
    async def ddl_get_remaining_tool(self, event: AstrMessageEvent, limit: int = 10):
        '''查看当前群尚未截止的 DDL。

        Args:
            limit(number): 最多返回多少条 DDL。
        '''
        return await LLMToolsMixin.ddl_get_remaining_tool(self, event, limit)

    @filter.llm_tool(name="ddl_get_due_within")
    async def ddl_get_due_within_tool(self, event: AstrMessageEvent, hours: int = 24):
        '''查看指定时间范围内即将截止的 DDL。

        Args:
            hours(number): 未来多少小时内截止。
        '''
        return await LLMToolsMixin.ddl_get_due_within_tool(self, event, hours)

    @filter.llm_tool(name="ddl_set_type_reminder")
    async def ddl_set_type_reminder_tool(self, event: AstrMessageEvent, ddl_type: str, rule_text: str):
        '''为某一类 DDL 设置提醒规则。

        如果返回 pending_future_task_count > 0，主 Agent 必须继续调用官方 future_task
        创建每一条 pending_future_tasks，并在创建成功后调用 ddl_mark_future_task_created。

        Args:
            ddl_type(string): DDL 类型关键词，例如作业、考试、讲座报告。
            rule_text(string): 提醒规则文本，例如"提前1天提醒""前一天晚上22点提醒"。
        '''
        return await LLMToolsMixin.ddl_set_type_reminder_tool(self, event, ddl_type, rule_text)

    @filter.llm_tool(name="ddl_get_reminder_rules")
    async def ddl_get_reminder_rules_tool(self, event: AstrMessageEvent):
        '''查看当前群已生效的分类提醒规则。'''
        return await LLMToolsMixin.ddl_get_reminder_rules_tool(self, event)

    @filter.llm_tool(name="ddl_get_pending_future_tasks")
    async def ddl_get_pending_future_tasks_tool(self, event: AstrMessageEvent, limit: int = 20):
        '''获取当前群需要由主 Agent 创建的官方 FutureTask 提醒计划。

        如果返回 count > 0，主 Agent 必须继续调用官方 future_task 创建每一条 items，
        并在创建成功后调用 ddl_mark_future_task_created。

        Args:
            limit(number): 最多返回多少个待创建的提醒任务。
        '''
        return await LLMToolsMixin.ddl_get_pending_future_tasks_tool(self, event, limit)

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
        return await LLMToolsMixin.ddl_mark_future_task_created_tool(
            self,
            event,
            fingerprint,
            remind_key,
            task_name,
        )
