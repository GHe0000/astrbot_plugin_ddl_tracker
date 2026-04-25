"""Helpers for reading plugin config values."""

from __future__ import annotations

from astrbot.api import AstrBotConfig


class PluginSettings:
    """Light wrapper around the config object provided by AstrBot."""

    def __init__(self, config: AstrBotConfig | dict | None):
        """Keep a dict-like config object for later reads."""
        self.config = config or {}

    def enabled(self) -> bool:
        """Return whether the demo plugin is enabled."""
        return bool(self.config.get("enabled", True))

    def timezone(self) -> str:
        """Return the configured timezone name."""
        return str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")

    def remind_before_minutes(self) -> int:
        """Return the configured reminder lead time."""
        value = self.config.get("remind_before_minutes", 60)
        try:
            return int(value)
        except Exception:
            return 60

    def admin_ids(self) -> list[str]:
        """Return the configured admin id list as strings."""
        raw_value = self.config.get("admin_ids", []) or []
        return [str(item) for item in raw_value]

    def log_config_on_start(self) -> bool:
        """Return whether the plugin should print config during startup."""
        debug_config = self.config.get("debug", {}) or {}
        return bool(debug_config.get("log_config_on_start", True))

    def debug_note(self) -> str:
        """Return the optional debug note from the nested config."""
        debug_config = self.config.get("debug", {}) or {}
        return str(debug_config.get("note", "") or "")

    def dump(self) -> dict:
        """Return a normalized dict that is easy to inspect."""
        return {
            "enabled": self.enabled(),
            "timezone": self.timezone(),
            "remind_before_minutes": self.remind_before_minutes(),
            "admin_ids": self.admin_ids(),
            "debug": {
                "log_config_on_start": self.log_config_on_start(),
                "note": self.debug_note(),
            },
        }
