"""基础工具函数，提供安全转换和 JSON 解析。"""

from __future__ import annotations

import json
from typing import Any


def safe_int(
    value: Any,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """安全转为 int，并按需裁剪范围。"""
    try:
        result = int(value)
    except Exception:
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    if maximum is not None and result > maximum:
        result = maximum
    return result


def safe_float(
    value: Any,
    default: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """安全转为 float，并按需裁剪范围。"""
    try:
        result = float(value)
    except Exception:
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    if maximum is not None and result > maximum:
        result = maximum
    return result


def safe_json_loads(text: str, default: dict[str, Any]) -> dict[str, Any]:
    """尽量从模型输出中解析出 JSON 对象。"""
    raw = str(text or "").strip()
    if not raw:
        return default

    if raw.startswith("```"):
        first_newline = raw.find("\n")
        last_fence = raw.rfind("```")
        if first_newline != -1 and last_fence != -1 and last_fence > first_newline:
            raw = raw[first_newline + 1:last_fence].strip()

    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else default
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                payload = json.loads(raw[start:end + 1])
                return payload if isinstance(payload, dict) else default
            except Exception:
                pass
    return default
