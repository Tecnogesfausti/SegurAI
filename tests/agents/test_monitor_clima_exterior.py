from __future__ import annotations

import asyncio
import datetime as dt
import unittest

from agents.base import AgentContext
from agents.monitor_clima_exterior import MonitorClimaExteriorAgent


class FakeLiveContext:
    async def get_context(self, **kwargs):
        return {
            "warnings": [],
            "weather": {
                "summary": "38.5 C; probabilidad lluvia prox. horas 70%; racha 55 km/h",
                "warnings": [],
                "data": {
                    "current": {
                        "temperature_2m": 38.5,
                        "relative_humidity_2m": 45,
                        "rain": 0,
                        "wind_gusts_10m": 55,
                    },
                    "hourly_preview": [{"precipitation_probability": 70}],
                },
            },
        }


class FakeMemory:
    def __init__(self) -> None:
        self.observations = []

    def add_observation(self, **kwargs) -> None:
        self.observations.append(kwargs)


class MonitorClimaExteriorAgentTest(unittest.TestCase):
    def test_run_builds_deterministic_recommendations(self) -> None:
        async def run() -> None:
            memory = FakeMemory()
            agent = MonitorClimaExteriorAgent()
            result = await agent.run(AgentContext(now=dt.datetime.now(dt.UTC), services={"live_context": FakeLiveContext(), "memory": memory}))
            self.assertTrue(result.ok)
            self.assertEqual(result.data["severity"], "alto")
            self.assertTrue(result.data["recommendations"])
            self.assertEqual(len(memory.observations), 1)
            self.assertEqual(memory.observations[0]["source"], "monitor_clima_exterior")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
