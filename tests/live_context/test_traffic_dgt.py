from __future__ import annotations

import asyncio
import unittest

import httpx

from services.live_context.config import LiveContextConfig
from services.live_context.http_client import LiveContextHTTPClient
from services.live_context.models import LocationConfig
from services.live_context.providers.traffic_dgt import DGTTrafficProvider, normalize_road


class DGTTrafficProviderTest(unittest.TestCase):
    def test_normalizes_json_incidents_near_torrent(self) -> None:
        async def run() -> None:
            payload = {
                "incidents": [
                    {
                        "id": "a7-1",
                        "road": "A7",
                        "title": "Retencion en A-7 sentido Valencia",
                        "description": "Retencion frecuente por incorporacion",
                        "severity": "retencion",
                        "lat": 39.43,
                        "lon": -0.46,
                    },
                    {
                        "id": "far",
                        "road": "A-3",
                        "title": "Incidencia lejos",
                        "lat": 40.4,
                        "lon": -3.7,
                    },
                ]
            }

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=payload)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test")
            provider = DGTTrafficProvider(LiveContextHTTPClient(client=client))
            config = LiveContextConfig(
                location=LocationConfig("Torrent", 39.4371, -0.4655, 20, "Europe/Madrid"),
                dgt_traffic_enabled=True,
                dgt_traffic_url="https://example.test/dgt.json",
            )
            result = await provider.fetch(config)
            await client.aclose()
            incidents = result["data"]["incidents"]
            self.assertEqual(len(incidents), 1)
            self.assertEqual(incidents[0]["road"], "A-7")
            self.assertEqual(incidents[0]["severity"], "warning")
            self.assertLess(incidents[0]["distance_km"], 3)

        asyncio.run(run())

    def test_missing_url_returns_warning_without_network(self) -> None:
        async def run() -> None:
            provider = DGTTrafficProvider()
            result = await provider.fetch(LiveContextConfig(dgt_traffic_enabled=True, dgt_traffic_url=""))
            self.assertEqual(result["warnings"], ["dgt_traffic_url_missing"])
            self.assertEqual(result["data"]["incidents"], [])

        asyncio.run(run())

    def test_normalize_road_accepts_a7_without_dash(self) -> None:
        self.assertEqual(normalize_road("A7"), "A-7")
        self.assertEqual(normalize_road("ap 7"), "AP-7")


if __name__ == "__main__":
    unittest.main()
