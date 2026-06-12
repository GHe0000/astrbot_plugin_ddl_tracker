from time import time

from .constants import STATE_FILE


class StateMixin:
    def _load_state(self) -> dict[str, dict]:
        import json

        if not STATE_FILE.exists():
            return {}
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, dict]):
        import json

        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _persist(self):
        self._save_state(self.state)

    def _normalize_loaded_state(self) -> bool:
        changed = False
        for group_id, group_state in list(self.state.items()):
            if not isinstance(group_state, dict):
                self.state[group_id] = {
                    "enabled": False,
                    "messages": [],
                    "ddl_items": [],
                    "reminder_rules": {},
                }
                changed = True
                continue

            if self._ensure_group_state_fields(group_state):
                changed = True
            if self._purge_expired_ddls(group_state) > 0:
                changed = True
        return changed

    def _prepare_group_state_for_read(self, group_id: str) -> dict:
        group_state = self.state.get(group_id, {})
        if not isinstance(group_state, dict):
            group_state = {}

        changed = self._ensure_group_state_fields(group_state)
        removed_count = self._purge_expired_ddls(group_state)
        if group_id in self.state and (changed or removed_count > 0):
            self.state[group_id] = group_state
            self._persist()
        return group_state

    def _ensure_group_state(self, group_id: str, event) -> dict:
        group_state = self.state.get(group_id, {})
        self._ensure_group_state_fields(group_state)
        group_state["unified_msg_origin"] = str(
            event.unified_msg_origin or group_state.get("unified_msg_origin") or ""
        )
        self.state[group_id] = group_state
        return group_state

    def _ensure_group_state_fields(self, group_state: dict) -> bool:
        changed = False
        if not isinstance(group_state.get("messages"), list):
            group_state["messages"] = []
            changed = True
        if not isinstance(group_state.get("ddl_items"), list):
            group_state["ddl_items"] = []
            changed = True
        if not isinstance(group_state.get("reminder_rules"), dict):
            group_state["reminder_rules"] = {}
            changed = True
        if "enabled" not in group_state:
            group_state["enabled"] = False
            changed = True
        if "unified_msg_origin" not in group_state:
            group_state["unified_msg_origin"] = ""
            changed = True
        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            if "future_task_name" not in item:
                item["future_task_name"] = ""
                changed = True
            if "future_task_actual_name" not in item:
                item["future_task_actual_name"] = ""
                changed = True
            if "future_task_job_id" not in item:
                item["future_task_job_id"] = ""
                changed = True
            if "future_task_remind_key" not in item:
                item["future_task_remind_key"] = ""
                changed = True
            if "future_task_remind_ts" not in item:
                item["future_task_remind_ts"] = 0
                changed = True
            if "future_task_recorded_at" not in item:
                item["future_task_recorded_at"] = 0
                changed = True
        return changed

    def _purge_expired_from_all_groups(self) -> int:
        removed_count = 0
        for group_state in self.state.values():
            if not isinstance(group_state, dict):
                continue
            self._ensure_group_state_fields(group_state)
            removed_count += self._purge_expired_ddls(group_state)
        return removed_count

    def _purge_expired_ddls(self, group_state: dict) -> int:
        items = group_state.get("ddl_items", [])
        if not isinstance(items, list) or not items:
            return 0

        now_ts = int(time())
        kept_items = []
        removed_count = 0
        for item in items:
            if not isinstance(item, dict):
                removed_count += 1
                continue
            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts <= 0 or deadline_ts <= now_ts:
                removed_count += 1
                continue
            kept_items.append(item)

        if removed_count > 0:
            group_state["ddl_items"] = kept_items
        return removed_count
