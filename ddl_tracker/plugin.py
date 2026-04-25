"""AstrBot plugin entrypoint for config loading only."""

from __future__ import annotations

import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .settings import PluginSettings


@register(
    "ddl_tracker",
    "Codex",
    "Config loading example for ddl_tracker",
    "1.3.0",
)
class DDLTrackerPlugin(Star):
    """Minimal plugin that only reads plugin config."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
        *args,
        **kwargs,
    ):
        """Store and normalize the config injected by AstrBot."""
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings(self.config)

        if self.settings.log_config_on_start():
            logger.info(
                "[ddl_tracker] loaded config: %s",
                json.dumps(self.settings.dump(), ensure_ascii=False),
            )

    @filter.command("ddl_config")
    async def ddl_config(self, event: AstrMessageEvent):
        """Show the parsed plugin config in the current chat."""
        yield event.plain_result(
            json.dumps(self.settings.dump(), ensure_ascii=False, indent=2)
        )
