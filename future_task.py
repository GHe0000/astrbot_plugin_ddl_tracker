import hashlib
from time import time

from utils import safe_int, format_ts


class FutureTaskMixin:
    def _get_pending_future_tasks(self, group_id: str, group_state: dict, limit: int) -> list[dict]:
        if not self._auto_remind_enabled():
            return []

        now_ts = int(time())
        pending_items = []
        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            deadline_ts = self._item_deadline_ts(item)
            if deadline_ts <= 0 or deadline_ts <= now_ts:
                continue

            remind_plan = self._get_item_remind_plan(group_state, item, deadline_ts)
            remind_ts = safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
            remind_key = str(remind_plan.get("remind_key") or "")
            if remind_ts <= now_ts or not remind_key:
                continue

            expected_task_name = self._build_future_task_name(group_id, item, remind_key)
            recorded_task_name = str(item.get("future_task_name") or "").strip()
            recorded_key = str(item.get("future_task_remind_key") or "").strip()
            recorded_ts = safe_int(item.get("future_task_remind_ts"), default=0, minimum=0)
            if (
                recorded_task_name == expected_task_name
                and recorded_key == remind_key
                and recorded_ts == remind_ts
            ):
                continue

            stale_task_names = []
            if recorded_task_name and recorded_task_name != expected_task_name:
                stale_task_names.append(recorded_task_name)

            pending_items.append(
                self._build_future_task_payload(
                    group_id=group_id,
                    group_state=group_state,
                    item=item,
                    deadline_ts=deadline_ts,
                    remind_plan=remind_plan,
                    task_name=expected_task_name,
                    stale_task_names=stale_task_names,
                )
            )

        pending_items.sort(key=lambda item: item.get("remind_ts", 0))
        return pending_items[:limit]

    def _build_future_task_payload(
        self,
        group_id: str,
        group_state: dict,
        item: dict,
        deadline_ts: int,
        remind_plan: dict,
        task_name: str,
        stale_task_names: list[str],
    ) -> dict:
        remind_ts = safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
        remind_key = str(remind_plan.get("remind_key") or "")
        deadline_display = (
            str(item.get("normalized_deadline") or "").strip()
            or str(item.get("deadline_text") or "").strip()
            or format_ts(deadline_ts)
        )
        rule_text = str(remind_plan.get("rule_text") or "").strip() or f"提前{self._remind_before_minutes()}分钟提醒"
        return {
            "fingerprint": str(item.get("fingerprint") or "").strip(),
            "type": str(item.get("type") or "").strip() or "其他",
            "title": str(item.get("title") or "").strip() or "未命名 DDL",
            "deadline_text": str(item.get("deadline_text") or "").strip(),
            "normalized_deadline": deadline_display,
            "deadline_ts": deadline_ts,
            "source_text": str(item.get("source_text") or "").strip(),
            "remind_key": remind_key,
            "rule_text": rule_text,
            "remind_ts": remind_ts,
            "remind_at": format_ts(remind_ts),
            "task_name": task_name,
            "task_note": self._build_future_task_note(item, deadline_display),
            "stale_task_names": stale_task_names,
            "group_id": group_id,
            "unified_msg_origin": str(group_state.get("unified_msg_origin") or ""),
        }

    def _build_future_task_name(self, group_id: str, item: dict, remind_key: str) -> str:
        raw = "|".join(
            [
                str(group_id or "").strip(),
                str(item.get("fingerprint") or "").strip(),
                str(remind_key or "").strip(),
            ]
        )
        suffix = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"ddl_tracker_{group_id}_{suffix}"

    def _build_future_task_note(self, item: dict, deadline_display: str) -> str:
        title = str(item.get("title") or "").strip() or "未命名 DDL"
        item_type = str(item.get("type") or "").strip() or "其他"
        source_text = str(item.get("source_text") or "").strip()
        return (
            "这是 ddl_tracker 插件生成的 DDL 提醒任务。"
            "请先确认该 DDL 仍然存在且尚未截止；如有需要，可调用 ddl_get_remaining 或 ddl_get_due_within 进行核对。"
            "确认后，直接在当前群里发送一条简短中文提醒，不要重新创建 future_task，不要闲聊。"
            f"提醒内容：{title}（{item_type}）将于 {deadline_display} 截止。"
            f"原始消息：{source_text}"
        )
