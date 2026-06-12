import re
from datetime import datetime, timedelta
from time import time

from .utils import safe_int, normalize_type_keyword, normalize_match_text, parse_number_token, apply_period_to_hour


class ReminderRulesMixin:
    def _extract_reminder_rule_from_text(self, raw_message: str) -> dict | None:
        message = str(raw_message or "").strip()
        if not message:
            return None

        compact = re.sub(r"\s+", "", message)
        compact = compact.replace("的", "")
        if "提醒" not in compact and "通知" not in compact:
            return None

        relative_match = re.search(
            r"(?P<ddl_type>[\u4e00-\u9fa5A-Za-z0-9]{1,20})(?:类)?(?:DDL)?(?:都|统一|全部)?提前"
            r"(?P<value>[0-9零一二两三四五六七八九十百半]+)"
            r"(?P<unit>分钟|小时|天|周|礼拜)(?:前)?(?:提醒|通知)",
            compact,
        )
        if relative_match:
            return self._build_relative_rule(
                ddl_type=relative_match.group("ddl_type"),
                value_text=relative_match.group("value"),
                unit_text=relative_match.group("unit"),
                rule_text=relative_match.group(0),
            )

        fixed_match = re.search(
            r"(?P<ddl_type>[\u4e00-\u9fa5A-Za-z0-9]{1,20})(?:类)?(?:DDL)?(?:都|统一|全部)?(?:在)?"
            r"(?:(?:前(?P<days>[0-9零一二两三四五六七八九十]+)天)|(?P<one_day>前一天)|(?P<same_day>当天))"
            r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
            r"(?P<hour>[0-9]{1,2})"
            r"(?:[点时:：](?P<minute>[0-9]{1,2}))?"
            r"(?:分)?(?:提醒|通知)",
            compact,
        )
        if fixed_match:
            if fixed_match.group("same_day"):
                days_before = 0
            elif fixed_match.group("one_day"):
                days_before = 1
            else:
                days_before = parse_number_token(fixed_match.group("days"))

            return self._build_fixed_clock_rule(
                ddl_type=fixed_match.group("ddl_type"),
                days_before=days_before,
                hour_text=fixed_match.group("hour"),
                minute_text=fixed_match.group("minute"),
                period_text=fixed_match.group("period"),
                rule_text=fixed_match.group(0),
            )
        return None

    def _build_reminder_rule_from_parts(self, ddl_type: str, rule_text: str) -> dict | None:
        type_text = str(ddl_type or "").strip()
        rule_part = str(rule_text or "").strip()
        if not type_text or not rule_part:
            return None

        combined = f"{type_text}{rule_part}"
        rule = self._extract_reminder_rule_from_text(combined)
        if rule:
            return rule

        compact = re.sub(r"\s+", "", rule_part)
        relative_match = re.fullmatch(
            r"提前(?P<value>[0-9零一二两三四五六七八九十百半]+)(?P<unit>分钟|小时|天|周|礼拜)(?:前)?(?:提醒|通知)?",
            compact,
        )
        if relative_match:
            return self._build_relative_rule(
                ddl_type=type_text,
                value_text=relative_match.group("value"),
                unit_text=relative_match.group("unit"),
                rule_text=rule_part,
            )

        fixed_match = re.fullmatch(
            r"(?:(?:前(?P<days>[0-9零一二两三四五六七八九十]+)天)|(?P<one_day>前一天)|(?P<same_day>当天))"
            r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
            r"(?P<hour>[0-9]{1,2})"
            r"(?:[点时:：](?P<minute>[0-9]{1,2}))?"
            r"(?:分)?(?:提醒|通知)?",
            compact,
        )
        if fixed_match:
            if fixed_match.group("same_day"):
                days_before = 0
            elif fixed_match.group("one_day"):
                days_before = 1
            else:
                days_before = parse_number_token(fixed_match.group("days"))
            return self._build_fixed_clock_rule(
                ddl_type=type_text,
                days_before=days_before,
                hour_text=fixed_match.group("hour"),
                minute_text=fixed_match.group("minute"),
                period_text=fixed_match.group("period"),
                rule_text=rule_part,
            )
        return None

    def _build_relative_rule(self, ddl_type: str, value_text: str, unit_text: str, rule_text: str) -> dict | None:
        target = normalize_type_keyword(ddl_type)
        value = parse_number_token(value_text)
        if not target or value <= 0:
            return None

        unit_minutes = {
            "分钟": 1,
            "小时": 60,
            "天": 1440,
            "周": 10080,
            "礼拜": 10080,
        }.get(str(unit_text or "").strip(), 0)
        offset_minutes = value * unit_minutes
        if offset_minutes <= 0:
            return None

        return {
            "type_keyword": target,
            "match_text": target.lower(),
            "mode": "relative",
            "offset_minutes": offset_minutes,
            "rule_text": str(rule_text or "").strip() or f"提前{value_text}{unit_text}提醒",
            "updated_at": int(time()),
        }

    def _build_fixed_clock_rule(
        self,
        ddl_type: str,
        days_before: int,
        hour_text: str,
        minute_text: str | None,
        period_text: str | None,
        rule_text: str,
    ) -> dict | None:
        target = normalize_type_keyword(ddl_type)
        hour = safe_int(hour_text, default=-1)
        minute = safe_int(minute_text, default=0, minimum=0)
        if not target or days_before < 0:
            return None

        hour = apply_period_to_hour(hour, str(period_text or "").strip())
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None

        return {
            "type_keyword": target,
            "match_text": target.lower(),
            "mode": "fixed_clock",
            "days_before": days_before,
            "hour": hour,
            "minute": minute,
            "period_text": str(period_text or "").strip(),
            "rule_text": str(rule_text or "").strip(),
            "updated_at": int(time()),
        }

    def _upsert_reminder_rule(self, group_state: dict, rule: dict) -> bool:
        rule_map = group_state.setdefault("reminder_rules", {})
        if not isinstance(rule_map, dict):
            group_state["reminder_rules"] = {}
            rule_map = group_state["reminder_rules"]

        key = str(rule.get("match_text") or normalize_type_keyword(rule.get("type_keyword"))).lower()
        if not key:
            return False
        existing = rule_map.get(key)
        if existing == rule:
            return False
        rule_map[key] = dict(rule)
        return True

    def _list_reminder_rules(self, group_state: dict) -> list[dict]:
        rule_map = group_state.get("reminder_rules", {})
        if not isinstance(rule_map, dict):
            return []
        return sorted(
            [dict(rule) for rule in rule_map.values() if isinstance(rule, dict)],
            key=lambda item: len(str(item.get("match_text") or item.get("type_keyword") or "")),
            reverse=True,
        )

    def _find_matching_reminder_rule(self, group_state: dict, item: dict) -> dict | None:
        haystacks = [
            normalize_match_text(item.get("type")),
            normalize_match_text(item.get("title")),
            normalize_match_text(item.get("source_text")),
        ]
        for rule in self._list_reminder_rules(group_state):
            match_text = str(rule.get("match_text") or "").strip().lower()
            if not match_text:
                continue
            for haystack in haystacks:
                if not haystack:
                    continue
                if haystack == match_text or match_text in haystack or haystack in match_text:
                    return rule
        return None

    def _get_item_remind_plan(self, group_state: dict, item: dict, deadline_ts: int | None = None) -> dict:
        final_deadline_ts = deadline_ts or self._item_deadline_ts(item)
        if final_deadline_ts <= 0:
            return {"remind_ts": 0, "remind_key": "", "rule_text": ""}

        rule = self._find_matching_reminder_rule(group_state, item)
        if rule:
            remind_ts = self._compute_rule_remind_ts(final_deadline_ts, rule)
            rule_key = self._build_rule_key(rule)
            rule_text = str(rule.get("rule_text") or "").strip()
        else:
            remind_ts = final_deadline_ts - self._remind_before_minutes() * 60
            rule_key = f"default:{self._remind_before_minutes()}"
            rule_text = f"提前{self._remind_before_minutes()}分钟提醒"

        if remind_ts >= final_deadline_ts:
            remind_ts = max(0, final_deadline_ts - 60)
        return {
            "remind_ts": remind_ts,
            "remind_key": rule_key,
            "rule_text": rule_text,
        }

    def _compute_rule_remind_ts(self, deadline_ts: int, rule: dict) -> int:
        mode = str(rule.get("mode") or "").strip()
        if mode == "relative":
            offset_minutes = safe_int(rule.get("offset_minutes"), default=0, minimum=0)
            return max(0, deadline_ts - offset_minutes * 60)

        if mode == "fixed_clock":
            deadline_dt = datetime.fromtimestamp(deadline_ts)
            days_before = safe_int(rule.get("days_before"), default=0, minimum=0)
            hour = safe_int(rule.get("hour"), default=0, minimum=0)
            minute = safe_int(rule.get("minute"), default=0, minimum=0)
            target_date = (deadline_dt - timedelta(days=days_before)).date()
            return int(
                datetime(
                    target_date.year,
                    target_date.month,
                    target_date.day,
                    hour,
                    minute,
                    0,
                ).timestamp()
            )
        return max(0, deadline_ts - self._remind_before_minutes() * 60)

    def _build_rule_key(self, rule: dict) -> str:
        mode = str(rule.get("mode") or "").strip()
        if mode == "relative":
            return f"relative:{str(rule.get('match_text') or '')}:{safe_int(rule.get('offset_minutes'), default=0, minimum=0)}"
        if mode == "fixed_clock":
            return (
                f"fixed:{str(rule.get('match_text') or '')}:"
                f"{safe_int(rule.get('days_before'), default=0, minimum=0)}:"
                f"{safe_int(rule.get('hour'), default=0, minimum=0)}:"
                f"{safe_int(rule.get('minute'), default=0, minimum=0)}"
            )
        return f"default:{self._remind_before_minutes()}"

    def _serialize_reminder_rule(self, rule: dict) -> dict:
        payload = {
            "type_keyword": str(rule.get("type_keyword") or "").strip(),
            "mode": str(rule.get("mode") or "").strip(),
            "rule_text": str(rule.get("rule_text") or "").strip(),
        }
        if payload["mode"] == "relative":
            payload["offset_minutes"] = safe_int(rule.get("offset_minutes"), default=0, minimum=0)
        elif payload["mode"] == "fixed_clock":
            payload["days_before"] = safe_int(rule.get("days_before"), default=0, minimum=0)
            payload["hour"] = safe_int(rule.get("hour"), default=0, minimum=0)
            payload["minute"] = safe_int(rule.get("minute"), default=0, minimum=0)
        return payload
