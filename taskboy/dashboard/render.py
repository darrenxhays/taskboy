"""redacted presentation helpers: everything leaving the api passes through the shared redactor."""

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from taskboy.redact import redactor

SECRET_KEY = re.compile(r"(?i)(secret|password|passwd|token|credential|private[_-]?key|api[_-]?key)")


def safe_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, default=str, sort_keys=True)
    return redactor.redact(value)


def bounded_text(value: object, limit: int = 10000) -> str:
    text = safe_text(value)
    return text if len(text) <= limit else text[:limit] + "\n[… truncated in dashboard …]"


def redact_value(value: object, key: str = "") -> object:
    # mask only string values under a secret-named key; numeric config (e.g. usage_limits.*_tokens) is not a secret
    if SECRET_KEY.search(key) and isinstance(value, str):
        return "••••••••" if value else "not configured"
    if is_dataclass(value):
        value = asdict(cast(Any, value))
    if isinstance(value, dict):
        return {str(item_key): redact_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return safe_text(value) if isinstance(value, str) else value


def redact_bounded_value(value: object, key: str = "") -> object:
    if SECRET_KEY.search(key) and isinstance(value, str):
        return "••••••••" if value else "not configured"
    if is_dataclass(value):
        value = asdict(cast(Any, value))
    if isinstance(value, dict):
        return {str(item_key): redact_bounded_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_bounded_value(item) for item in value]
    return bounded_text(value) if isinstance(value, str) else value
