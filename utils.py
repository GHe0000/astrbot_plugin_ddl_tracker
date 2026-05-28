import hashlib
import json
import re
from datetime import datetime


def safe_json_loads(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}

    if raw.startswith("```"):
        first_newline = raw.find("\n")
        last_fence = raw.rfind("```")
        if first_newline != -1 and last_fence != -1 and last_fence > first_newline:
            raw = raw[first_newline + 1:last_fence].strip()

    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                payload = json.loads(raw[start:end + 1])
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
    return {}


def safe_int(value, default: int = 0, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except Exception:
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    return result


def format_ts(timestamp: int) -> str:
    ts = safe_int(timestamp, default=0, minimum=0)
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def build_fingerprint(item: dict) -> str:
    normalized_deadline = item.get("normalized_deadline") or item.get("deadline_text") or ""
    raw = "|".join(
        [
            str(item.get("type") or "").strip().lower(),
            str(item.get("title") or "").strip().lower(),
            str(normalized_deadline).strip().lower(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_deadline_ts(deadline_text: str) -> int:
    raw = str(deadline_text or "").strip().replace("/", "-").replace("T", " ")
    if not raw:
        return 0

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y-%m-%d-%H:%M:%S",
        "%Y-%m-%d-%H:%M",
        "%Y-%m-%d-%H",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%Y-%m-%d":
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return int(parsed.timestamp())
        except ValueError:
            continue
    return 0


def parse_number_token(raw_text: str) -> int:
    text = str(raw_text or "").strip()
    if not text:
        return 0
    if text == "半":
        return 0
    try:
        return int(text)
    except ValueError:
        pass

    mapping = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + mapping.get(text[1:], 0)
    if text.endswith("十"):
        return mapping.get(text[0], 0) * 10
    if "十" in text:
        left, right = text.split("十", 1)
        return mapping.get(left, 0) * 10 + mapping.get(right, 0)
    return mapping.get(text, 0)


def apply_period_to_hour(hour: int, period_text: str) -> int:
    if hour < 0:
        return hour
    period = str(period_text or "").strip()
    if period in {"凌晨"}:
        return 0 if hour == 12 else hour
    if period in {"早上", "上午"}:
        return 0 if hour == 12 else hour
    if period in {"中午"}:
        if hour == 12:
            return 12
        return hour + 12 if 1 <= hour <= 11 else hour
    if period in {"下午", "晚上"}:
        if 1 <= hour <= 11:
            return hour + 12
        return hour
    return hour


def normalize_type_keyword(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    text = re.sub(r"(类|事项|任务|ddl)$", "", text, flags=re.IGNORECASE)
    return text.strip()


def normalize_match_text(raw_text: str) -> str:
    return re.sub(r"\s+", "", str(raw_text or "").strip()).lower()


def format_remaining(remaining_seconds: int) -> str:
    seconds = safe_int(remaining_seconds, default=0)
    if seconds <= 0:
        return "已截止"

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分钟")
    if not parts:
        parts.append("不足1分钟")
    return "".join(parts[:2])
