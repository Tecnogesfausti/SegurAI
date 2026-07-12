from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LocationConfig:
    name: str
    lat: float
    lon: float
    radius_km: float
    timezone: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "radius_km": self.radius_km,
            "timezone": self.timezone,
        }


@dataclass
class NormalizedContext:
    domain: str
    location: LocationConfig
    source: str
    fetched_at: dt.datetime
    expires_at: dt.datetime
    is_stale: bool
    confidence: float
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "location": self.location.as_dict(),
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(timespec="seconds"),
            "expires_at": self.expires_at.isoformat(timespec="seconds"),
            "is_stale": self.is_stale,
            "confidence": self.confidence,
            "summary": self.summary,
            "data": self.data,
            "warnings": self.warnings,
        }
