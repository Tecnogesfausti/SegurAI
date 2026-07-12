from __future__ import annotations

import os
from dataclasses import dataclass, field

from services.live_context.models import LocationConfig


@dataclass(frozen=True)
class LiveContextConfig:
    location: LocationConfig = field(
        default_factory=lambda: LocationConfig(
            name=os.getenv("LIVE_CONTEXT_LOCATION", "Torrent, Valencia, España"),
            lat=float(os.getenv("LIVE_CONTEXT_LAT", "39.4371")),
            lon=float(os.getenv("LIVE_CONTEXT_LON", "-0.4655")),
            radius_km=float(os.getenv("LIVE_CONTEXT_RADIUS_KM", "20")),
            timezone=os.getenv("LIVE_CONTEXT_TIMEZONE", "Europe/Madrid"),
        )
    )
    http_timeout_seconds: float = float(os.getenv("LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS", "15"))
    max_retries: int = int(os.getenv("LIVE_CONTEXT_MAX_RETRIES", "2"))
    max_concurrency: int = int(os.getenv("LIVE_CONTEXT_MAX_CONCURRENCY", "4"))
    open_meteo_enabled: bool = os.getenv("OPEN_METEO_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    dgt_traffic_enabled: bool = os.getenv("DGT_TRAFFIC_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    dgt_traffic_url: str = os.getenv("DGT_TRAFFIC_URL", "")
    open_meteo_air_quality_enabled: bool = os.getenv("OPEN_METEO_AIR_QUALITY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

    @property
    def air_quality_locations(self) -> list[LocationConfig]:
        default = "Torrent|39.4371|-0.4655;Valencia|39.4699|-0.3763"
        return parse_air_quality_locations(os.getenv("AIR_QUALITY_LOCATIONS", default), self.location)

    @classmethod
    def from_env(cls) -> "LiveContextConfig":
        return cls()


def parse_air_quality_locations(value: str, fallback: LocationConfig) -> list[LocationConfig]:
    rows: list[LocationConfig] = []
    for item in value.split(';'):
        parts = [part.strip() for part in item.split('|')]
        if len(parts) != 3 or not all(parts):
            continue
        try:
            rows.append(LocationConfig(parts[0], float(parts[1]), float(parts[2]), fallback.radius_km, fallback.timezone))
        except ValueError:
            continue
    return rows or [fallback]
