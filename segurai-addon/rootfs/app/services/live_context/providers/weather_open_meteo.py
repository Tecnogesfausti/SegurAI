from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from services.live_context.config import LiveContextConfig
from services.live_context.http_client import LiveContextHTTPClient
from services.live_context.models import NormalizedContext
from services.live_context.providers.base import LiveContextProvider


class OpenMeteoWeatherProvider(LiveContextProvider):
    domain = "weather"
    source = "open_meteo"
    ttl_seconds = 600
    url = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, http_client: LiveContextHTTPClient | None = None) -> None:
        self.http_client = http_client or LiveContextHTTPClient()

    async def fetch(self, config: LiveContextConfig) -> dict[str, Any]:
        location = config.location
        params = {
            "latitude": location.lat,
            "longitude": location.lon,
            "timezone": location.timezone,
            "current": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "precipitation",
                    "rain",
                    "weather_code",
                    "wind_speed_10m",
                    "wind_gusts_10m",
                ]
            ),
            "hourly": ",".join(["precipitation_probability", "temperature_2m", "wind_gusts_10m"]),
            "daily": ",".join(["sunrise", "sunset", "uv_index_max"]),
            "forecast_days": 2,
        }
        payload = await self.http_client.get_json(self.url, params=params)
        now = dt.datetime.now(ZoneInfo(location.timezone))
        expires_at = now + dt.timedelta(seconds=self.ttl_seconds)
        current = payload.get("current") or {}
        hourly = payload.get("hourly") or {}
        daily = payload.get("daily") or {}
        summary = build_weather_summary(current=current, hourly=hourly)
        normalized = NormalizedContext(
            domain=self.domain,
            location=location,
            source=self.source,
            fetched_at=now,
            expires_at=expires_at,
            is_stale=False,
            confidence=0.9,
            summary=summary,
            data={
                "current": current,
                "hourly_preview": compact_hourly(hourly, limit=6),
                "daily": daily,
                "units": {
                    "current": payload.get("current_units") or {},
                    "hourly": payload.get("hourly_units") or {},
                    "daily": payload.get("daily_units") or {},
                },
            },
            warnings=[],
        )
        return normalized.as_dict()


def compact_hourly(hourly: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    times = hourly.get("time") or []
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(times[:limit]):
        rows.append(
            {
                "time": timestamp,
                "temperature_2m": value_at(hourly, "temperature_2m", index),
                "precipitation_probability": value_at(hourly, "precipitation_probability", index),
                "wind_gusts_10m": value_at(hourly, "wind_gusts_10m", index),
            }
        )
    return rows


def value_at(data: dict[str, Any], key: str, index: int) -> Any:
    values = data.get(key) or []
    return values[index] if index < len(values) else None


def build_weather_summary(*, current: dict[str, Any], hourly: dict[str, Any]) -> str:
    temperature = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    rain = current.get("rain") or current.get("precipitation") or 0
    wind_gust = current.get("wind_gusts_10m")
    probabilities = [value for value in (hourly.get("precipitation_probability") or [])[:6] if isinstance(value, (int, float))]
    max_rain_probability = max(probabilities) if probabilities else None
    parts = []
    if temperature is not None:
        parts.append(f"{temperature} C")
    if humidity is not None:
        parts.append(f"humedad {humidity}%")
    if max_rain_probability is not None:
        parts.append(f"probabilidad lluvia prox. horas {max_rain_probability}%")
    if rain:
        parts.append(f"lluvia actual {rain} mm")
    if wind_gust is not None:
        parts.append(f"racha {wind_gust} km/h")
    return "; ".join(parts) if parts else "Clima exterior actualizado sin datos destacados."
