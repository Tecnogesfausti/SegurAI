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

    @classmethod
    def from_env(cls) -> "LiveContextConfig":
        return cls()
