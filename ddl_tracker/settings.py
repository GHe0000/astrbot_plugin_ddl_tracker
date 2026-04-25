"""插件配置读取与时间相关设置。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig

from .utils import safe_int


class PluginSettings:
    """统一封装插件配置读取。"""

    def __init__(self, config: AstrBotConfig | dict | None):
        """保存 AstrBot 注入的原始配置对象。"""
        self.config = config or {}

    def plugin_name(self) -> str:
        """返回插件内部使用的固定名称。"""
        return "ddl_tracker"

    def admin_ids(self) -> set[str]:
        """返回额外管理员 ID 集合。"""
        return {str(item) for item in (self.config.get("admin_ids", []) or [])}

    def llm_provider_id(self) -> str:
        """返回显式配置的模型 Provider ID。"""
        return str(self.config.get("llm_provider_id", "") or "").strip()

    def timezone_name(self) -> str:
        """返回插件使用的时区名称。"""
        return str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")

    def timezone(self):
        """返回时区对象，失败时回退到 UTC。"""
        try:
            return ZoneInfo(self.timezone_name())
        except Exception:
            return timezone.utc

    def now_ts(self) -> int:
        """返回当前秒级时间戳。"""
        return int(time.time())

    def format_ts(self, ts: int) -> str:
        """按插件时区格式化时间戳。"""
        if ts <= 0:
            return "未执行"
        return datetime.fromtimestamp(ts, tz=self.timezone()).strftime("%Y-%m-%d %H:%M:%S")

    def extract_interval_minutes(self) -> int:
        """返回自动抽取间隔。"""
        return self.get_int("extract_interval_minutes", default=30, minimum=1)

    def max_messages_per_extract(self) -> int:
        """返回单次抽取的最大消息数。"""
        return self.get_int("max_messages_per_extract", default=200, minimum=1)

    def default_remind_before_minutes(self) -> int:
        """返回默认提前提醒分钟数。"""
        return self.get_int("default_remind_before_minutes", default=60, minimum=0)

    def auto_remind_enabled(self) -> bool:
        """返回是否启用自动提醒。"""
        return self.get_bool("auto_remind_enabled", default=True)

    def send_extract_summary_back_to_group(self) -> bool:
        """返回是否把抽取摘要回发到群里。"""
        return self.get_bool("send_extract_summary_back_to_group", default=False)

    def tick_interval_seconds(self) -> int:
        """返回后台轮询间隔秒数。"""
        return self.get_int("tick_interval_seconds", default=30, minimum=5)

    def default_rule(self) -> dict:
        """构造默认提醒规则。"""
        return {
            "category": "默认",
            "remind_type": "offset",
            "offset_minutes": self.default_remind_before_minutes(),
            "days_before": 0,
            "fixed_hour": -1,
            "fixed_minute": -1,
            "source_text": "",
            "source_message_ids": [],
            "created_by_sender_id": "",
        }

    def get_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置并兼容字符串值。"""
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def get_int(
        self,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """读取整数配置并做范围裁剪。"""
        return safe_int(
            self.config.get(key, default),
            default=default,
            minimum=minimum,
            maximum=maximum,
        )
