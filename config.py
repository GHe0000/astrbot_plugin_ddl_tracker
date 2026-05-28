from constants import DEFAULT_EXTRACT_PROMPT, DEFAULT_REMIND_BEFORE_MINUTES, DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES
from utils import safe_int


class ConfigMixin:
    def _auto_extract_enabled(self) -> bool:
        return bool(self.config.get("auto_extract_enabled", True))

    def _auto_remind_enabled(self) -> bool:
        return bool(self.config.get("auto_remind_enabled", True))

    def _remind_before_minutes(self) -> int:
        return safe_int(
            self.config.get("remind_before_minutes", DEFAULT_REMIND_BEFORE_MINUTES),
            default=DEFAULT_REMIND_BEFORE_MINUTES,
            minimum=1,
        )

    def _auto_extract_interval_minutes(self) -> int:
        return safe_int(
            self.config.get(
                "auto_extract_interval_minutes",
                DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES,
            ),
            default=DEFAULT_AUTO_EXTRACT_INTERVAL_MINUTES,
            minimum=1,
        )

    def _extract_prompt(self) -> str:
        prompt = str(self.config.get("extract_prompt", "") or "").strip()
        return prompt or DEFAULT_EXTRACT_PROMPT

    def _dump_config(self) -> dict:
        return {
            "llm_provider_id": str(self.config.get("llm_provider_id", "") or "").strip(),
            "auto_extract_enabled": self._auto_extract_enabled(),
            "auto_extract_interval_minutes": self._auto_extract_interval_minutes(),
            "auto_remind_enabled": self._auto_remind_enabled(),
            "remind_before_minutes": self._remind_before_minutes(),
            "extract_prompt": self._extract_prompt(),
        }
