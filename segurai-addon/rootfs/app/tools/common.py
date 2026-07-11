from __future__ import annotations

import json
from typing import Any


def compact_json(value: Any, max_chars: int = 6000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncado]"
    return text
