from time import time

from constants import MAX_MESSAGES_PER_GROUP, COMMAND_NAMES
from utils import safe_int, format_ts, build_fingerprint, parse_deadline_ts, format_remaining


class DDLItemMixin:
    def _normalize_item(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        normalized = {
            "message_index": safe_int(item.get("message_index"), default=0, minimum=0),
            "type": str(item.get("type") or "").strip() or "其他",
            "title": str(item.get("title") or "").strip(),
            "deadline_text": str(item.get("deadline_text") or "").strip(),
            "normalized_deadline": str(item.get("normalized_deadline") or "").strip(),
            "source_text": str(item.get("source_text") or "").strip(),
        }
        if not normalized["title"]:
            return None
        return normalized

    def _item_deadline_ts(self, item: dict) -> int:
        stored_deadline_ts = safe_int(item.get("deadline_ts"), default=0, minimum=0)
        if stored_deadline_ts > 0:
            return stored_deadline_ts

        for key in ("normalized_deadline", "deadline_text"):
            deadline_ts = parse_deadline_ts(str(item.get(key) or "").strip())
            if deadline_ts > 0:
                item["deadline_ts"] = deadline_ts
                return deadline_ts
        return 0

    def _merge_ddl_items(self, group_state: dict, parsed_result: dict) -> tuple[int, int]:
        existing_items = group_state.setdefault("ddl_items", [])
        existing_by_fp = {
            str(item.get("fingerprint") or ""): item
            for item in existing_items
            if str(item.get("fingerprint") or "")
        }
        added_count = 0
        updated_count = 0

        for raw_item in parsed_result.get("items") or []:
            item = self._normalize_item(raw_item)
            if not item:
                continue

            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts > 0 and deadline_ts <= int(time()):
                continue

            fingerprint = build_fingerprint(item)
            item["fingerprint"] = fingerprint
            item["deadline_ts"] = deadline_ts
            item["updated_at"] = int(time())

            existing = existing_by_fp.get(fingerprint)
            if existing is None:
                item["created_at"] = item["updated_at"]
                item["last_reminded_at"] = 0
                item["last_reminded_deadline_ts"] = 0
                item["last_reminded_key"] = ""
                item["future_task_name"] = ""
                item["future_task_remind_key"] = ""
                item["future_task_remind_ts"] = 0
                item["future_task_recorded_at"] = 0
                existing_items.append(item)
                existing_by_fp[fingerprint] = item
                added_count += 1
                continue

            merged = dict(existing)
            for key in ("type", "title", "deadline_text", "normalized_deadline", "source_text", "message_index"):
                new_value = item.get(key)
                old_value = merged.get(key)
                if new_value and (not old_value or len(str(new_value)) >= len(str(old_value))):
                    merged[key] = new_value
            merged_deadline_ts = self._item_deadline_ts(merged)
            merged["deadline_ts"] = merged_deadline_ts
            if safe_int(merged.get("last_reminded_deadline_ts"), default=0, minimum=0) != merged_deadline_ts:
                merged["last_reminded_at"] = 0
                merged["last_reminded_deadline_ts"] = 0
                merged["last_reminded_key"] = ""
            merged.setdefault("future_task_name", "")
            merged.setdefault("future_task_remind_key", "")
            merged.setdefault("future_task_remind_ts", 0)
            merged.setdefault("future_task_recorded_at", 0)
            merged["updated_at"] = item["updated_at"]

            if merged != existing:
                existing.clear()
                existing.update(merged)
                updated_count += 1

        group_state["ddl_items"] = existing_items
        return added_count, updated_count

    def _select_recent_messages(self, messages: list[dict], lookback_minutes: int) -> list[dict]:
        if not messages:
            return []

        cutoff_ts = int(time()) - lookback_minutes * 60
        result = []
        for item in messages:
            text = str(item.get("message_text") or "").strip()
            if not text:
                continue
            if self._is_plugin_command_text(text):
                continue
            if safe_int(item.get("message_ts"), default=0, minimum=0) < cutoff_ts:
                continue
            result.append(item)
        return result

    def _get_nearest_ddls(self, group_state: dict, limit: int) -> list[dict]:
        now_ts = int(time())
        sortable_items = []
        for item in group_state.get("ddl_items", []):
            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts <= 0 or deadline_ts < now_ts:
                continue
            sortable_items.append(
                {
                    "title": str(item.get("title") or "").strip() or "未命名 DDL",
                    "type": str(item.get("type") or "").strip() or "其他",
                    "deadline_text": str(item.get("deadline_text") or "").strip(),
                    "normalized_deadline": (
                        str(item.get("normalized_deadline") or "").strip()
                        or str(item.get("deadline_text") or "").strip()
                        or format_ts(deadline_ts)
                    ),
                    "source_text": str(item.get("source_text") or "").strip(),
                    "deadline_ts": deadline_ts,
                }
            )

        sortable_items.sort(key=lambda item: item["deadline_ts"])
        return sortable_items[:limit]

    def _get_due_within_ddls(self, group_state: dict, hours: int) -> list[dict]:
        now_ts = int(time())
        limit_ts = now_ts + safe_int(hours, default=24, minimum=1) * 3600
        result = []
        for item in self._get_nearest_ddls(group_state, limit=9999):
            if item["deadline_ts"] <= limit_ts:
                result.append(item)
        return result

    def _tool_item_payload(self, item: dict) -> dict:
        deadline_ts = self._item_deadline_ts(item)
        return {
            "fingerprint": str(item.get("fingerprint") or "").strip(),
            "type": str(item.get("type") or "").strip() or "其他",
            "title": str(item.get("title") or "").strip() or "未命名 DDL",
            "deadline_text": str(item.get("deadline_text") or "").strip(),
            "normalized_deadline": (
                str(item.get("normalized_deadline") or "").strip()
                or str(item.get("deadline_text") or "").strip()
                or format_ts(deadline_ts)
            ),
            "source_text": str(item.get("source_text") or "").strip(),
            "remaining": format_remaining(deadline_ts - int(time())) if deadline_ts > 0 else "未知",
        }

    def _is_plugin_command_text(self, raw_message: str) -> bool:
        message = str(raw_message or "").strip()
        if not message:
            return False
        return message.split()[0].lower() in COMMAND_NAMES
