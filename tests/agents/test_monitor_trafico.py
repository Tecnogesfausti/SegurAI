from __future__ import annotations

import asyncio
import datetime as dt
import os
import unittest

from agents.base import AgentContext
from agents.monitor_trafico import MonitorTraficoAgent


class FakeLiveContext:
    def __init__(self, traffic, warnings=None) -> None:
        self.traffic = traffic
        self.warnings = warnings or []

    async def get_context(self, **kwargs):
        return {"warnings": self.warnings, "traffic": self.traffic}


class FakeMemory:
    def __init__(self) -> None:
        self.observations = []

    def add_observation(self, **kwargs) -> None:
        self.observations.append(kwargs)


class MonitorTraficoAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = {key: os.environ.get(key) for key in os.environ if key.startswith("MONITOR_TRAFICO_")}
        for key in list(os.environ):
            if key.startswith("MONITOR_TRAFICO_"):
                os.environ.pop(key)

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("MONITOR_TRAFICO_"):
                os.environ.pop(key)
        for key, value in self.old_env.items():
            if value is not None:
                os.environ[key] = value

    def test_alerts_for_a7_retention(self) -> None:
        async def run() -> None:
            traffic = {
                "summary": "1 incidencia",
                "warnings": [],
                "data": {
                    "incidents": [
                        {
                            "id": "a7-1",
                            "road": "A-7",
                            "title": "Retencion en A-7",
                            "description": "Retencion sentido Valencia",
                            "severity": "warning",
                            "distance_km": 4.2,
                        },
                        {"id": "cv35", "road": "CV-35", "title": "Lejos", "severity": "warning"},
                    ]
                },
            }
            memory = FakeMemory()
            agent = MonitorTraficoAgent()
            result = await agent.run(
                AgentContext(
                    now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                    services={"live_context": FakeLiveContext(traffic), "memory": memory},
                )
            )
            self.assertTrue(result.ok)
            self.assertEqual(len(result.data["alerts"]), 1)
            self.assertEqual(result.data["alerts"][0]["road"], "A-7")
            self.assertEqual(result.data["action_proposal"]["type"], "check_alternative_route")
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_deduplicates_repeated_traffic_alerts(self) -> None:
        async def run() -> None:
            traffic = {
                "data": {"incidents": [{"id": "a7-1", "road": "A7", "title": "Retencion", "severity": "warning"}]},
                "warnings": [],
            }
            memory = FakeMemory()
            agent = MonitorTraficoAgent()
            context = AgentContext(
                now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                services={"live_context": FakeLiveContext(traffic), "memory": memory},
            )
            first = await agent.run(context)
            second = await agent.run(context)
            self.assertTrue(first.ok)
            self.assertTrue(second.data["deduplicated"])
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_provider_not_configured_is_warning_not_fake_alert(self) -> None:
        async def run() -> None:
            traffic = {"data": {"incidents": []}, "warnings": ["dgt_traffic_url_missing"]}
            result = await MonitorTraficoAgent().run(
                AgentContext(
                    now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                    services={"live_context": FakeLiveContext(traffic)},
                )
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["alerts"], [])
            self.assertIn("sin datos fiables", result.message)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
