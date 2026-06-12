import hashlib
from datetime import datetime
from time import time

from .utils import safe_int, format_run_at, format_ts


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
            recorded_actual_task_name = str(item.get("future_task_actual_name") or "").strip()
            recorded_job_id = str(item.get("future_task_job_id") or "").strip()
            recorded_key = str(item.get("future_task_remind_key") or "").strip()
            recorded_ts = safe_int(item.get("future_task_remind_ts"), default=0, minimum=0)
            if (
                recorded_task_name == expected_task_name
                and recorded_key == remind_key
                and recorded_ts == remind_ts
            ):
                continue

            stale_task_names = []
            stale_task_job_ids = []
            if recorded_task_name and recorded_task_name != expected_task_name:
                stale_task_names.append(recorded_actual_task_name or recorded_task_name)
                if recorded_job_id:
                    stale_task_job_ids.append(recorded_job_id)

            pending_items.append(
                self._build_future_task_payload(
                    group_id=group_id,
                    group_state=group_state,
                    item=item,
                    deadline_ts=deadline_ts,
                    remind_plan=remind_plan,
                    task_name=expected_task_name,
                    stale_task_names=stale_task_names,
                    stale_task_job_ids=stale_task_job_ids,
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
        stale_task_job_ids: list[str],
    ) -> dict:
        remind_ts = safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
        remind_key = str(remind_plan.get("remind_key") or "")
        deadline_display = (
            str(item.get("normalized_deadline") or "").strip()
            or str(item.get("deadline_text") or "").strip()
            or format_ts(deadline_ts)
        )
        task_note = self._build_future_task_note(item, deadline_display)
        rule_text = str(remind_plan.get("rule_text") or "").strip() or f"提前{self._remind_before_minutes()}分钟提醒"
        payload = {
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
            "run_at": format_run_at(remind_ts),
            "task_name": task_name,
            "task_note": task_note,
            "stale_task_names": stale_task_names,
            "stale_task_job_ids": stale_task_job_ids,
            "group_id": group_id,
            "unified_msg_origin": str(group_state.get("unified_msg_origin") or ""),
        }
        payload["future_task_arguments"] = {
            "action": "create",
            "name": task_name,
            "run_once": True,
            "run_at": payload["run_at"],
            "note": task_note,
        }
        payload["mark_created_arguments"] = {
            "fingerprint": payload["fingerprint"],
            "remind_key": remind_key,
            "task_name": task_name,
        }
        return payload

    async def _sync_pending_future_tasks(
        self,
        group_id: str,
        group_state: dict,
        source: str,
        limit: int = 20,
    ) -> dict:
        if not self._auto_remind_enabled():
            return {"ok": False, "reason": "auto_remind_enabled is disabled"}
        if not self._auto_create_future_tasks_enabled():
            return {"ok": False, "reason": "auto_create_future_tasks is disabled"}

        cron_mgr = getattr(getattr(self, "context", None), "cron_manager", None)
        if cron_mgr is None:
            return {"ok": False, "reason": "AstrBot cron_manager is not available"}

        pending_items = self._get_pending_future_tasks(group_id, group_state, limit=limit)
        if not pending_items:
            return {"ok": True, "source": source, "pending_count": 0, "created_count": 0}

        created_items = []
        failed_items = []
        deleted_stale_count = 0
        for pending in pending_items:
            task_name = str(pending.get("task_name") or "").strip()
            try:
                for stale_job_id in pending.get("stale_task_job_ids") or []:
                    if not stale_job_id:
                        continue
                    await cron_mgr.delete_job(str(stale_job_id))
                    deleted_stale_count += 1

                unified_msg_origin = str(pending.get("unified_msg_origin") or "").strip()
                if not unified_msg_origin:
                    failed_items.append({"task_name": task_name, "reason": "unified_msg_origin is empty"})
                    continue

                run_at = datetime.fromisoformat(str(pending.get("run_at") or ""))
                task_note = str(pending.get("task_note") or "").strip()
                job = await cron_mgr.add_active_job(
                    name=task_name,
                    cron_expression=None,
                    payload={
                        "session": unified_msg_origin,
                        "sender_id": "ddl_tracker",
                        "note": task_note,
                        "origin": "tool",
                        "source": "ddl_tracker",
                    },
                    description=task_note,
                    run_once=True,
                    run_at=run_at,
                )
                record_result = self._record_future_task_created(
                    group_state=group_state,
                    group_id=group_id,
                    fingerprint=str(pending.get("fingerprint") or ""),
                    remind_key=str(pending.get("remind_key") or ""),
                    actual_task_name=str(getattr(job, "name", "") or task_name),
                    job_id=str(getattr(job, "job_id", "") or ""),
                )
                if not record_result.get("ok"):
                    job_id = str(getattr(job, "job_id", "") or "")
                    if job_id:
                        await cron_mgr.delete_job(job_id)
                    failed_items.append(
                        {
                            "task_name": task_name,
                            "reason": record_result.get("reason", "record failed"),
                        }
                    )
                    continue
                created_items.append(
                    {
                        "task_name": task_name,
                        "job_id": str(getattr(job, "job_id", "") or ""),
                        "remind_at": pending.get("remind_at"),
                    }
                )
            except Exception as exc:
                failed_items.append({"task_name": task_name, "reason": str(exc)})

        if created_items or deleted_stale_count:
            self.state[group_id] = group_state
            self._persist()

        return {
            "ok": not failed_items,
            "source": source,
            "pending_count": len(pending_items),
            "created_count": len(created_items),
            "deleted_stale_count": deleted_stale_count,
            "failed_count": len(failed_items),
            "created_items": created_items,
            "failed_items": failed_items,
        }

    def _record_future_task_created(
        self,
        group_state: dict,
        group_id: str,
        fingerprint: str,
        remind_key: str,
        actual_task_name: str,
        job_id: str = "",
    ) -> dict:
        fingerprint_text = str(fingerprint or "").strip()
        remind_key_text = str(remind_key or "").strip()
        actual_task_name_text = str(actual_task_name or "").strip()
        if not fingerprint_text or not remind_key_text or not actual_task_name_text:
            return {"ok": False, "reason": "fingerprint、remind_key、task_name 不能为空"}

        for item in group_state.get("ddl_items", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("fingerprint") or "").strip() != fingerprint_text:
                continue

            deadline_ts = self._item_deadline_ts(item)
            remind_plan = self._get_item_remind_plan(group_state, item, deadline_ts)
            current_remind_key = str(remind_plan.get("remind_key") or "")
            current_remind_ts = safe_int(remind_plan.get("remind_ts"), default=0, minimum=0)
            expected_task_name = self._build_future_task_name(group_id, item, current_remind_key)
            if current_remind_key != remind_key_text:
                return {
                    "ok": False,
                    "reason": "当前 DDL 的提醒计划已变化，请重新获取待创建任务",
                    "current_remind_key": current_remind_key,
                    "requested_remind_key": remind_key_text,
                }

            item["future_task_name"] = expected_task_name
            item["future_task_actual_name"] = actual_task_name_text
            item["future_task_job_id"] = str(job_id or "").strip()
            item["future_task_remind_key"] = current_remind_key
            item["future_task_remind_ts"] = current_remind_ts
            item["future_task_recorded_at"] = int(time())
            return {
                "ok": True,
                "task_name": actual_task_name_text,
                "expected_task_name": expected_task_name,
                "job_id": str(job_id or "").strip(),
                "remind_at": format_ts(current_remind_ts),
            }

        return {"ok": False, "reason": "未找到对应 DDL"}

    def _build_future_task_sync_instruction(self, pending_items: list[dict]) -> dict:
        if not pending_items:
            return {
                "required": False,
                "message": "没有待创建的官方 future_task 任务。",
            }

        return {
            "required": True,
            "message": (
                "主 Agent 必须继续为 pending_future_tasks 中的每一项调用官方 future_task 工具创建一次性任务；"
                "创建成功后，必须立刻调用 ddl_mark_future_task_created 写回绑定记录。"
            ),
            "sequence": [
                {
                    "step": 1,
                    "tool": "future_task",
                    "arguments_from": "pending_future_tasks[].future_task_arguments",
                },
                {
                    "step": 2,
                    "tool": "ddl_mark_future_task_created",
                    "arguments_from": "pending_future_tasks[].mark_created_arguments",
                },
            ],
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
