"""提醒时间计算、结果落库和后台提醒循环。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
import sqlite3

from astrbot.api import logger

from .data_ops import prepare_deadline


def compute_remind_meta(deadline_ts: int, rule: dict, settings) -> dict:
    """根据规则计算提醒时间和规则描述。"""
    if rule.get("remind_type") == "fixed_day_before_time":
        tzinfo = settings.timezone()
        deadline_dt = datetime.fromtimestamp(deadline_ts, tz=tzinfo)
        target_date = (deadline_dt - timedelta(days=max(int(rule.get("days_before", 0)), 0))).date()
        remind_dt = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            int(rule.get("fixed_hour", 0)),
            int(rule.get("fixed_minute", 0)),
            tzinfo=tzinfo,
        )
        remind_at_ts = int(remind_dt.timestamp())
        return {
            "remind_at_ts": min(remind_at_ts, deadline_ts),
            "rule_type": "fixed_day_before_time",
            "rule_value": json.dumps(
                {
                    "days_before": int(rule.get("days_before", 0)),
                    "fixed_hour": int(rule.get("fixed_hour", 0)),
                    "fixed_minute": int(rule.get("fixed_minute", 0)),
                },
                ensure_ascii=False,
            ),
        }

    offset_minutes = max(int(rule.get("offset_minutes", 0)), 0)
    remind_at_ts = deadline_ts - offset_minutes * 60
    return {
        "remind_at_ts": min(remind_at_ts, deadline_ts),
        "rule_type": "offset",
        "rule_value": json.dumps({"offset_minutes": offset_minutes}, ensure_ascii=False),
    }


def apply_rule_to_existing_deadlines(store, settings, group_id: str, category: str, now_ts: int | None = None, conn: sqlite3.Connection | None = None) -> int:
    """把某类规则回刷到已有未完成 DDL。"""
    effective_now = now_ts or settings.now_ts()
    rule = store.get_rule(group_id=group_id, category=category, conn=conn)
    if not rule:
        return 0

    deadlines = store.list_pending_deadlines_by_category(group_id=group_id, category=category, conn=conn)
    updated = 0
    for deadline in deadlines:
        meta = compute_remind_meta(deadline["deadline_ts"], rule, settings)
        reminded = deadline["reminded"] if deadline["deadline_ts"] <= effective_now else False
        store.update_deadline_reminder(
            deadline_id=deadline["id"],
            meta=meta,
            reminded=reminded,
            now_ts=effective_now,
            conn=conn,
        )
        updated += 1
    return updated


def save_extract_result(store, settings, group_id: str, messages: list[dict], result: dict) -> dict:
    """保存一次模型抽取结果，并同步刷新提醒时间。"""
    now_ts = settings.now_ts()
    fallback_message_ids = [message["id"] for message in messages]
    saved = {
        "inserted_deadlines": 0,
        "upserted_rules": 0,
        "recalculated_deadlines": 0,
    }

    with store.connection() as conn:
        for rule in result.get("reminder_rules", []):
            changed = store.upsert_reminder_rule(group_id=group_id, rule=rule, now_ts=now_ts, conn=conn)
            if changed:
                saved["upserted_rules"] += 1

        for item in result.get("deadlines", []):
            prepared = prepare_deadline(item, fallback_message_ids)
            if not prepared:
                continue
            if store.deadline_exists(group_id=group_id, fingerprint=prepared["fingerprint"], conn=conn):
                continue

            rule = store.get_rule(group_id=group_id, category=prepared["category"], conn=conn) or settings.default_rule()
            meta = compute_remind_meta(prepared["deadline_ts"], rule, settings)
            status = "expired" if prepared["deadline_ts"] <= now_ts else "pending"
            store.insert_deadline(
                group_id=group_id,
                deadline=prepared,
                meta=meta,
                status=status,
                now_ts=now_ts,
                conn=conn,
            )
            saved["inserted_deadlines"] += 1

        if saved["upserted_rules"] > 0:
            categories = {rule["category"] for rule in result.get("reminder_rules", []) if rule.get("category")}
            for category in categories:
                saved["recalculated_deadlines"] += apply_rule_to_existing_deadlines(
                    store=store,
                    settings=settings,
                    group_id=group_id,
                    category=category,
                    now_ts=now_ts,
                    conn=conn,
                )

        conn.commit()

    return saved


def build_summary_push(report: dict) -> str:
    """把抽取报告整理成群里可发送的摘要文本。"""
    summary = str(report.get("summary") or "").strip()
    if not summary:
        return ""
    return (
        "【DDL 摘要】\n"
        f"{summary}\n\n"
        f"本次新增 DDL: {report.get('inserted_deadlines', 0)} 条\n"
        f"更新提醒规则: {report.get('upserted_rules', 0)} 条"
    )


class ReminderLoop:
    """驱动定时抽取和自动提醒的后台循环。"""

    def __init__(self, store, settings, send_text):
        """保存循环依赖的存储、配置和发送函数。"""
        self.store = store
        self.settings = settings
        self.send_text = send_text

    async def tick(self, run_group_extract):
        """执行一次后台轮询。"""
        now_ts = self.settings.now_ts()
        self.store.mark_overdue_deadlines(now_ts)

        for group in self.store.list_enabled_groups():
            if now_ts - group["last_extract_at"] < self.settings.extract_interval_minutes() * 60:
                continue

            report = await run_group_extract(group["group_id"], force=False)
            if (
                report.get("success")
                and report.get("summary")
                and report.get("processed_message_count", 0) > 0
                and self.settings.send_extract_summary_back_to_group()
            ):
                await self.send_text(report["unified_msg_origin"], build_summary_push(report))

        if self.settings.auto_remind_enabled():
            await self.scan_and_remind(now_ts)

    async def scan_and_remind(self, now_ts: int):
        """扫描到点的 DDL 并向群里发送提醒。"""
        for item in self.store.list_due_reminders(now_ts):
            try:
                remain_seconds = max(item["deadline_ts"] - now_ts, 0)
                remain_hours = remain_seconds // 3600
                remain_minutes = (remain_seconds % 3600) // 60
                await self.send_text(
                    item["unified_msg_origin"],
                    "【DDL 提醒】\n"
                    f"类别：{item['category']}\n"
                    f"事项：{item['description']}\n"
                    f"截止时间：{self.settings.format_ts(item['deadline_ts'])}\n"
                    f"本次提醒时间：{self.settings.format_ts(item['remind_at_ts'])}\n"
                    f"剩余时间：{remain_hours} 小时 {remain_minutes} 分钟",
                )
                self.store.mark_deadline_reminded(item["id"], now_ts)
            except Exception as exc:
                logger.exception(f"[ddl_tracker] remind failed ddl_id={item['id']}: {exc}")
