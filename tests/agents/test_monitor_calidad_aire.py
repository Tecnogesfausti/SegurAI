from __future__ import annotations

import asyncio
import datetime as dt
import os
import unittest

from agents.base import AgentContext
from agents.monitor_calidad_aire import MonitorCalidadAireAgent


class FakeLiveContext:
    def __init__(self, air_quality, warnings=None) -> None:
        self.air_quality = air_quality
        self.warnings = warnings or []

    async def get_context(self, **kwargs):
        return {"warnings": self.warnings, "air_quality": self.air_quality}


class FakeMemory:
    def __init__(self) -> None:
        self.observations = []

    def add_observation(self, **kwargs) -> None:
        self.observations.append(kwargs)


class MonitorCalidadAireAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = {key: os.environ.get(key) for key in os.environ if key.startswith("MONITOR_CALIDAD_AIRE_")}
        for key in list(os.environ):
            if key.startswith("MONITOR_CALIDAD_AIRE_"):
                os.environ.pop(key)

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("MONITOR_CALIDAD_AIRE_"):
                os.environ.pop(key)
        for key, value in self.old_env.items():
            if value is not None:
                os.environ[key] = value

    def test_alerts_when_valencia_air_quality_is_bad(self) -> None:
        async def run() -> None:
            air_quality = {
                "warnings": [],
                "data": {
                    "locations": [
                        {"name": "Torrent", "european_aqi": 35, "category": "razonable", "current": {}},
                        {"name": "Valencia", "european_aqi": 88, "category": "muy_mala", "current": {}},
                    ]
                },
            }
            memory = FakeMemory()
            result = await MonitorCalidadAireAgent().run(
                AgentContext(
                    now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                    services={"live_context": FakeLiveContext(air_quality), "memory": memory},
                )
            )
            self.assertTrue(result.ok)
            self.assertEqual(len(result.data["alerts"]), 1)
            self.assertEqual(result.data["alerts"][0]["name"], "Valencia")
            self.assertIn("evitar ventilar", " ".join(result.data["recommendations"]).lower())
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_deduplicates_repeated_air_quality_alerts(self) -> None:
        async def run() -> None:
            air_quality = {"warnings": [], "data": {"locations": [{"name": "Valencia", "european_aqi": 88, "category": "muy_mala"}]}}
            memory = FakeMemory()
            agent = MonitorCalidadAireAgent()
            context = AgentContext(
                now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                services={"live_context": FakeLiveContext(air_quality), "memory": memory},
            )
            first = await agent.run(context)
            second = await agent.run(context)
            self.assertTrue(first.ok)
            self.assertTrue(second.data["deduplicated"])
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_missing_provider_returns_warning_message(self) -> None:
        async def run() -> None:
            air_quality = {"warnings": ["provider_disabled:air_quality"], "data": {"locations": []}}
            result = await MonitorCalidadAireAgent().run(
                AgentContext(
                    now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                    services={"live_context": FakeLiveContext(air_quality)},
                )
            )
            self.assertTrue(result.ok)
            self.assertIn("sin datos fiables", result.message)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
