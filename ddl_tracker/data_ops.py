"""数据清洗、归一化和输出整理逻辑。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .utils import safe_float, safe_int


CATEGORY_ALIASES = {
    "homework": "作业",
    "assignment": "作业",
    "quiz": "考试",
    "exam": "考试",
    "test": "考试",
    "presentation": "讲座报告",
    "report": "讲座报告",
    "lecture": "讲座报告",
    "seminar": "讲座报告",
    "lab": "实验",
    "experiment": "实验",
    "registration": "报名",
    "signup": "报名",
}

RULE_TYPE_ALIASES = {
    "offset": "offset",
    "advance": "offset",
    "before": "offset",
    "fixed_day_before_time": "fixed_day_before_time",
    "previous_day_time": "fixed_day_before_time",
    "fixed": "fixed_day_before_time",
}


def normalize_text(text: str) -> str:
    """清理多余空白并返回紧凑文本。"""
    return " ".join(str(text or "").strip().split())


def normalize_category(text: str) -> str:
    """把类别别名归一化为统一中文类别。"""
    value = normalize_text(text)
    if not value:
        return ""
    return CATEGORY_ALIASES.get(value.lower(), value)


def normalize_rule_type(remind_type: str) -> str:
    """把提醒规则类型归一化为内部值。"""
    value = normalize_text(remind_type).lower()
    return RULE_TYPE_ALIASES.get(value, "")


def normalize_message_ids(value: Any, fallback: Iterable[int] = ()) -> list[int]:
    """把消息 ID 列表整理成干净的整数数组。"""
    ids: list[int] = []
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        for item in value:
            item_id = safe_int(item, default=0, minimum=0)
            if item_id > 0:
                ids.append(item_id)
    if ids:
        return ids

    fallback_ids: list[int] = []
    for item in fallback:
        item_id = safe_int(item, default=0, minimum=0)
        if item_id > 0:
            fallback_ids.append(item_id)
    return fallback_ids


def build_deadline_fingerprint(category: str, description: str, deadline_ts: int) -> str:
    """为 DDL 构造去重指纹。"""
    raw = f"{category}|{description}|{deadline_ts}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_conversation_text(messages: list[dict], format_ts) -> str:
    """把消息列表拼成适合送给模型的文本。"""
    lines: list[str] = []
    for message in messages:
        sender_name = message.get("sender_name") or "未知用户"
        sender_role = message.get("sender_role") or ""
        role_suffix = f"/{sender_role}" if sender_role else ""
        lines.append(
            f"[msg_id={message['id']}][{format_ts(message['message_ts'])}]"
            f"[{sender_name}{role_suffix}] {message['message_text']}"
        )
    return "\n".join(lines)


def parse_extract_result(payload: dict) -> dict:
    """清洗模型输出，留下可入库的结构。"""
    result = {
        "summary": str(payload.get("summary") or ""),
        "deadlines": [],
        "reminder_rules": [],
    }

    for item in payload.get("deadlines") or []:
        description = normalize_text(str(item.get("description") or ""))
        deadline_ts = safe_int(item.get("deadline_ts"), default=0, minimum=0)
        if not description or deadline_ts <= 0:
            continue

        result["deadlines"].append(
            {
                "description": description,
                "category": normalize_category(str(item.get("category") or "其他")) or "其他",
                "deadline_text": str(item.get("deadline_text") or ""),
                "deadline_ts": deadline_ts,
                "confidence": safe_float(item.get("confidence"), default=0.8, minimum=0.0, maximum=1.0),
                "source_text": str(item.get("source_text") or ""),
                "source_message_ids": normalize_message_ids(item.get("source_message_ids")),
            }
        )

    for item in payload.get("reminder_rules") or []:
        category = normalize_category(str(item.get("category") or ""))
        remind_type = normalize_rule_type(str(item.get("remind_type") or ""))
        if not category or not remind_type:
            continue

        offset_minutes = safe_int(item.get("offset_minutes"), default=0, minimum=0)
        days_before = safe_int(item.get("days_before"), default=1, minimum=0)
        fixed_hour = safe_int(item.get("fixed_hour"), default=-1)
        fixed_minute = safe_int(item.get("fixed_minute"), default=-1)

        if remind_type == "offset" and offset_minutes <= 0:
            continue
        if remind_type == "fixed_day_before_time" and not (0 <= fixed_hour <= 23 and 0 <= fixed_minute <= 59):
            continue

        result["reminder_rules"].append(
            {
                "category": category,
                "remind_type": remind_type,
                "offset_minutes": offset_minutes,
                "days_before": days_before,
                "fixed_hour": fixed_hour,
                "fixed_minute": fixed_minute,
                "source_text": str(item.get("source_text") or ""),
                "source_message_ids": normalize_message_ids(item.get("source_message_ids")),
                "created_by_sender_id": "",
            }
        )

    return result


def build_rule_from_input(
    category: str,
    remind_type: str,
    offset_minutes: int = 0,
    days_before: int = 1,
    fixed_hour: int = -1,
    fixed_minute: int = -1,
    source_text: str = "",
    source_message_ids: list[int] | None = None,
    created_by_sender_id: str = "",
) -> dict | None:
    """把手动输入整理成内部提醒规则。"""
    normalized_category = normalize_category(category)
    normalized_remind_type = normalize_rule_type(remind_type)
    if not normalized_category or not normalized_remind_type:
        return None

    if normalized_remind_type == "offset":
        offset_minutes = safe_int(offset_minutes, default=0, minimum=1)
        return {
            "category": normalized_category,
            "remind_type": normalized_remind_type,
            "offset_minutes": offset_minutes,
            "days_before": 0,
            "fixed_hour": -1,
            "fixed_minute": -1,
            "source_text": source_text,
            "source_message_ids": source_message_ids or [],
            "created_by_sender_id": created_by_sender_id,
        }

    fixed_hour = safe_int(fixed_hour, default=-1)
    fixed_minute = safe_int(fixed_minute, default=-1)
    if not (0 <= fixed_hour <= 23 and 0 <= fixed_minute <= 59):
        return None

    return {
        "category": normalized_category,
        "remind_type": normalized_remind_type,
        "offset_minutes": 0,
        "days_before": safe_int(days_before, default=1, minimum=0),
        "fixed_hour": fixed_hour,
        "fixed_minute": fixed_minute,
        "source_text": source_text,
        "source_message_ids": source_message_ids or [],
        "created_by_sender_id": created_by_sender_id,
    }


def prepare_deadline(item: dict, fallback_message_ids: list[int]) -> dict | None:
    """把抽取出的 DDL 整理成待入库结构。"""
    description = normalize_text(str(item.get("description") or ""))
    category = normalize_category(str(item.get("category") or "其他")) or "其他"
    deadline_ts = safe_int(item.get("deadline_ts"), default=0, minimum=0)
    if not description or deadline_ts <= 0:
        return None

    source_message_ids = normalize_message_ids(item.get("source_message_ids"), fallback_message_ids)
    return {
        "description": description,
        "category": category,
        "deadline_text": str(item.get("deadline_text") or ""),
        "deadline_ts": deadline_ts,
        "source_text": str(item.get("source_text") or ""),
        "source_message_ids_json": json.dumps(source_message_ids, ensure_ascii=False),
        "confidence": safe_float(item.get("confidence"), default=0.8, minimum=0.0, maximum=1.0),
        "fingerprint": build_deadline_fingerprint(category, description, deadline_ts),
    }


def serialize_deadline(row: dict, format_ts) -> dict:
    """把数据库中的 DDL 记录整理成输出结构。"""
    return {
        "id": row["id"],
        "description": row["description"],
        "category": row["category"],
        "deadline_ts": row["deadline_ts"],
        "deadline_at": format_ts(row["deadline_ts"]),
        "remind_at_ts": row["remind_at_ts"],
        "remind_at": format_ts(row["remind_at_ts"]),
        "reminded": bool(row["reminded"]),
        "confidence": float(row.get("confidence") or 0),
    }


def serialize_rule(rule: dict) -> dict:
    """把内部提醒规则裁剪成对外输出字段。"""
    return {
        "category": rule["category"],
        "remind_type": rule["remind_type"],
        "offset_minutes": rule.get("offset_minutes", 0),
        "days_before": rule.get("days_before", 0),
        "fixed_hour": rule.get("fixed_hour", -1),
        "fixed_minute": rule.get("fixed_minute", -1),
    }


def rule_to_text(rule: dict) -> str:
    """把提醒规则转成适合群聊展示的文本。"""
    if rule.get("remind_type") == "offset":
        return f"{rule['category']}: 提前 {rule.get('offset_minutes', 0)} 分钟提醒"
    return (
        f"{rule['category']}: 截止前 {rule.get('days_before', 0)} 天 "
        f"{int(rule.get('fixed_hour', 0)):02d}:{int(rule.get('fixed_minute', 0)):02d} 提醒"
    )
