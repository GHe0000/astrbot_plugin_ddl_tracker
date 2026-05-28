from __future__ import annotations

import asyncio
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register

from config import ConfigMixin
from ddl_item import DDLItemMixin
from extraction import ExtractionMixin
from future_task import FutureTaskMixin
from llm_tools import LLMToolsMixin
from reminder_rules import ReminderRulesMixin
from state_store import StateMixin
from commands import CommandsMixin


@register(
    "ddl_tracker",
    "Codex",
    "单文件 DDL 调试插件",
    "0.8.0",
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
