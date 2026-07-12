from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    value: dict[str, Any]
    expires_at: dt.datetime

    def is_fresh(self, now: dt.datetime) -> bool:
        return now < self.expires_at


class TTLCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str, now: dt.datetime) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None or not entry.is_fresh(now):
            return None
        return entry.value

    def get_stale(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        return entry.value if entry else None

    def set(self, key: str, value: dict[str, Any], expires_at: dt.datetime) -> None:
        self._entries[key] = CacheEntry(value=value, expires_at=expires_at)
