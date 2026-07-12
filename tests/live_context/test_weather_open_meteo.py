from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from services.live_context.config import LiveContextConfig
from services.live_context.http_client import LiveContextHTTPClient
from services.live_context.providers.weather_open_meteo import OpenMeteoWeatherProvider


class WeatherOpenMeteoProviderTest(unittest.TestCase):
    def test_fetch_normalizes_weather_payload(self) -> None:
        async def run() -> None:
            async def handler(request: httpx.Request) -> httpx.Response:
                self.assertIn("latitude", str(request.url))
                return httpx.Response(
                    200,
                    json={
                        "current": {
                            "temperature_2m": 31.5,
                            "relative_humidity_2m": 55,
                            "rain": 0,
                            "wind_gusts_10m": 28,
                        },
                        "current_units": {"temperature_2m": "°C"},
                        "hourly": {
                            "time": ["2026-07-12T12:00", "2026-07-12T13:00"],
                            "temperature_2m": [31.5, 32.0],
                            "precipitation_probability": [10, 70],
                            "wind_gusts_10m": [28, 31],
                        },
                        "hourly_units": {},
                        "daily": {"sunrise": ["2026-07-12T06:45"], "sunset": ["2026-07-12T21:28"]},
                        "daily_units": {},
                    },
                )

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider = OpenMeteoWeatherProvider(LiveContextHTTPClient(client=client))
            result = await provider.fetch(LiveContextConfig.from_env())
            await client.aclose()
            self.assertEqual(result["domain"], "weather")
            self.assertEqual(result["source"], "open_meteo")
            self.assertFalse(result["is_stale"])
            self.assertIn("31.5 C", result["summary"])
            self.assertEqual(result["data"]["hourly_preview"][1]["precipitation_probability"], 70)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
