from __future__ import annotations

import asyncio
import unittest

import httpx

from services.live_context.config import LiveContextConfig
from services.live_context.http_client import LiveContextHTTPClient
from services.live_context.models import LocationConfig
from services.live_context.providers.air_quality_open_meteo import OpenMeteoAirQualityProvider, european_aqi_category


class OpenMeteoAirQualityProviderTest(unittest.TestCase):
    def test_fetches_torrent_and_valencia(self) -> None:
        async def run() -> None:
            responses = [
                {"current": {"time": "2026-07-12T12:00", "european_aqi": 35, "pm10": 20, "pm2_5": 8, "nitrogen_dioxide": 12, "ozone": 80}, "hourly": {"time": ["t1"], "european_aqi": [35]}},
                {"current": {"time": "2026-07-12T12:00", "european_aqi": 85, "pm10": 55, "pm2_5": 28, "nitrogen_dioxide": 40, "ozone": 120}, "hourly": {"time": ["t1"], "european_aqi": [85]}},
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=responses.pop(0))

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test")
            provider = OpenMeteoAirQualityProvider(LiveContextHTTPClient(client=client))
            config = LiveContextConfig(
                location=LocationConfig("Torrent", 39.4371, -0.4655, 20, "Europe/Madrid"),
            )
            result = await provider.fetch(config)
            await client.aclose()
            locations = result["data"]["locations"]
            self.assertEqual([item["name"] for item in locations], ["Torrent", "Valencia"])
            self.assertEqual(locations[0]["category"], "razonable")
            self.assertEqual(locations[1]["category"], "muy_mala")
            self.assertIn("Valencia", result["summary"])

        asyncio.run(run())

    def test_categories(self) -> None:
        self.assertEqual(european_aqi_category(10), "buena")
        self.assertEqual(european_aqi_category(70), "mala")
        self.assertEqual(european_aqi_category(120), "extremadamente_mala")


if __name__ == "__main__":
    unittest.main()
