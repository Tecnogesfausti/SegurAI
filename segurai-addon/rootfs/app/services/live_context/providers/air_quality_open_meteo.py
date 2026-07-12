from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from services.live_context.config import LiveContextConfig
from services.live_context.http_client import LiveContextHTTPClient
from services.live_context.models import LocationConfig, NormalizedContext
from services.live_context.providers.base import LiveContextProvider


class OpenMeteoAirQualityProvider(LiveContextProvider):
    domain = "air_quality"
    source = "open_meteo_air_quality"
    ttl_seconds = 1800
    url = "https://air-quality-api.open-meteo.com/v1/air-quality"

    def __init__(self, http_client: LiveContextHTTPClient | None = None) -> None:
        self.http_client = http_client or LiveContextHTTPClient()

    async def fetch(self, config: LiveContextConfig) -> dict[str, Any]:
        locations = getattr(config, "air_quality_locations", None) or [config.location]
        now = dt.datetime.now(ZoneInfo(config.location.timezone))
        expires_at = now + dt.timedelta(seconds=self.ttl_seconds)
        results = await asyncio.gather(*(self._fetch_location(location) for location in locations), return_exceptions=True)
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for location, result in zip(locations, results):
            if isinstance(result, Exception):
                warnings.append(f"{location.name}: {result.__class__.__name__}: {result}")
                continue
            rows.append(result)
        normalized = NormalizedContext(
            domain=self.domain,
            location=config.location,
            source=self.source,
            fetched_at=now,
            expires_at=expires_at,
            is_stale=False,
            confidence=0.82 if rows else 0.25,
            summary=build_summary(rows),
            data={"locations": rows},
            warnings=warnings,
        )
        return normalized.as_dict()

    async def _fetch_location(self, location: LocationConfig) -> dict[str, Any]:
        params = {
            "latitude": location.lat,
            "longitude": location.lon,
            "timezone": location.timezone,
            "current": ",".join(["european_aqi", "pm10", "pm2_5", "nitrogen_dioxide", "ozone", "dust"]),
            "hourly": ",".join(["european_aqi", "pm10", "pm2_5", "nitrogen_dioxide", "ozone"]),
            "forecast_days": 1,
        }
        payload = await self.http_client.get_json(self.url, params=params)
        current = payload.get("current") or {}
        hourly = payload.get("hourly") or {}
        aqi = as_float(current.get("european_aqi"))
        return {
            "name": location.name,
            "lat": location.lat,
            "lon": location.lon,
            "source": self.source,
            "type": "forecast",
            "european_aqi": aqi,
            "category": european_aqi_category(aqi),
            "current": {
                "european_aqi": aqi,
                "pm10": as_float(current.get("pm10")),
                "pm2_5": as_float(current.get("pm2_5")),
                "nitrogen_dioxide": as_float(current.get("nitrogen_dioxide")),
                "ozone": as_float(current.get("ozone")),
                "dust": as_float(current.get("dust")),
                "time": current.get("time"),
            },
            "hourly_preview": compact_hourly(hourly, limit=6),
        }


def compact_hourly(hourly: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    times = hourly.get("time") or []
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(times[:limit]):
        rows.append(
            {
                "time": timestamp,
                "european_aqi": value_at(hourly, "european_aqi", index),
                "pm10": value_at(hourly, "pm10", index),
                "pm2_5": value_at(hourly, "pm2_5", index),
                "nitrogen_dioxide": value_at(hourly, "nitrogen_dioxide", index),
                "ozone": value_at(hourly, "ozone", index),
            }
        )
    return rows


def value_at(data: dict[str, Any], key: str, index: int) -> Any:
    values = data.get(key) or []
    return values[index] if index < len(values) else None


def build_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Calidad del aire sin datos disponibles."
    worst = max(rows, key=lambda row: row.get("european_aqi") or -1)
    parts = [f"{row['name']}: AQI {row.get('european_aqi')} ({row.get('category')})" for row in rows]
    return f"Calidad del aire: {'; '.join(parts)}. Peor zona: {worst['name']}."


def european_aqi_category(value: float | None) -> str:
    if value is None:
        return "sin_datos"
    if value <= 20:
        return "buena"
    if value <= 40:
        return "razonable"
    if value <= 60:
        return "moderada"
    if value <= 80:
        return "mala"
    if value <= 100:
        return "muy_mala"
    return "extremadamente_mala"


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
