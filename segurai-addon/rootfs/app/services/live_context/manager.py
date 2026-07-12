from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from services.live_context.cache import TTLCache
from services.live_context.config import LiveContextConfig
from services.live_context.providers.base import LiveContextProvider
from services.live_context.providers.weather_open_meteo import OpenMeteoWeatherProvider
from services.live_context.providers.traffic_dgt import DGTTrafficProvider


class LiveContextManager:
    def __init__(self, config: LiveContextConfig | None = None, providers: list[LiveContextProvider] | None = None) -> None:
        self.config = config or LiveContextConfig.from_env()
        self.cache = TTLCache()
        self.providers: dict[str, LiveContextProvider] = {}
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        if providers is not None:
            for provider in providers:
                self.register(provider)
        else:
            self._register_default_providers()

    def _register_default_providers(self) -> None:
        if self.config.open_meteo_enabled:
            self.register(OpenMeteoWeatherProvider())
        if self.config.dgt_traffic_enabled:
            self.register(DGTTrafficProvider())

    def register(self, provider: LiveContextProvider) -> None:
        self.providers[provider.domain] = provider

    async def get_context(
        self,
        domains: list[str],
        location: str | None = None,
        radius_km: float | None = None,
        max_items_per_domain: int = 10,
    ) -> dict[str, Any]:
        del max_items_per_domain
        warnings: list[str] = []
        results: dict[str, Any] = {
            "location": self._location_payload(location=location, radius_km=radius_km),
            "warnings": warnings,
        }
        tasks = [self._get_domain(domain, warnings) for domain in domains]
        domain_results = await asyncio.gather(*tasks)
        for domain, value in domain_results:
            if value is not None:
                results[domain] = value
        return results

    async def refresh(self, domain: str) -> dict[str, Any]:
        provider = self.providers.get(domain)
        if provider is None:
            return {"domain": domain, "warnings": [f"provider_disabled:{domain}"]}
        async with self._semaphore:
            value = await provider.fetch(self.config)
        self.cache.set(self._cache_key(domain), value, self._parse_expires_at(value))
        return value

    async def health(self) -> dict[str, Any]:
        return {
            "location": self.config.location.as_dict(),
            "providers": sorted(self.providers),
            "open_meteo_enabled": self.config.open_meteo_enabled,
            "dgt_traffic_enabled": self.config.dgt_traffic_enabled,
        }

    async def _get_domain(self, domain: str, warnings: list[str]) -> tuple[str, dict[str, Any] | None]:
        now = dt.datetime.now(ZoneInfo(self.config.location.timezone))
        key = self._cache_key(domain)
        cached = self.cache.get(key, now)
        if cached is not None:
            return domain, cached
        try:
            return domain, await self.refresh(domain)
        except Exception as exc:  # noqa: BLE001
            stale = self.cache.get_stale(key)
            warning = f"{domain}: {exc.__class__.__name__}: {exc}"
            warnings.append(warning)
            if stale is not None:
                stale = dict(stale)
                stale["is_stale"] = True
                stale.setdefault("warnings", []).append(warning)
                return domain, stale
            return domain, {"domain": domain, "warnings": [warning], "is_stale": True, "data": {}}

    def _cache_key(self, domain: str) -> str:
        loc = self.config.location
        return f"{domain}:{loc.lat}:{loc.lon}:{loc.radius_km}"

    def _parse_expires_at(self, value: dict[str, Any]) -> dt.datetime:
        raw = value.get("expires_at")
        if isinstance(raw, str):
            return dt.datetime.fromisoformat(raw)
        return dt.datetime.now(ZoneInfo(self.config.location.timezone))

    def _location_payload(self, *, location: str | None, radius_km: float | None) -> dict[str, Any]:
        payload = self.config.location.as_dict()
        if location:
            payload["requested_name"] = location
        if radius_km is not None:
            payload["requested_radius_km"] = radius_km
        return payload
