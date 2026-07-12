from __future__ import annotations

import asyncio
import datetime as dt
import os
import unittest

from agents.base import AgentContext
from agents.monitor_temperatura import MonitorTemperaturaAgent


class FakeHAClient:
    def __init__(self, states, histories=None) -> None:
        self.states = states
        self.histories = histories or {}

    async def get_states(self):
        return self.states

    async def get_history(self, *, entity_id, start_time, end_time):
        return self.histories.get(entity_id, [])


class FakeMemory:
    def __init__(self) -> None:
        self.observations = []

    def add_observation(self, **kwargs) -> None:
        self.observations.append(kwargs)


class FakeSegurAIWithoutHA:
    has_homeassistant_rest = False


class MonitorTemperaturaAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = {key: os.environ.get(key) for key in os.environ if key.startswith("MONITOR_TEMPERATURA_")}
        for key in list(os.environ):
            if key.startswith("MONITOR_TEMPERATURA_"):
                os.environ.pop(key)

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("MONITOR_TEMPERATURA_"):
                os.environ.pop(key)
        for key, value in self.old_env.items():
            if value is not None:
                os.environ[key] = value

    def test_detects_out_of_range_and_rapid_change(self) -> None:
        async def run() -> None:
            now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
            states = [
                {
                    "entity_id": "sensor.salon_temperatura",
                    "state": "38.2",
                    "last_updated": (now - dt.timedelta(minutes=2)).isoformat(),
                    "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"},
                }
            ]
            histories = {
                "sensor.salon_temperatura": [
                    {"state": "33.0", "last_updated": (now - dt.timedelta(minutes=30)).isoformat()},
                    {"state": "38.2", "last_updated": now.isoformat()},
                ]
            }
            memory = FakeMemory()
            agent = MonitorTemperaturaAgent()
            result = await agent.run(
                AgentContext(now=now, services={"ha_client": FakeHAClient(states, histories), "memory": memory})
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.data["alerts"][0]["severity"], "critical")
            issue_types = {issue["type"] for issue in result.data["alerts"][0]["issues"]}
            self.assertIn("above_range", issue_types)
            self.assertIn("rapid_change", issue_types)
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_deduplicates_repeated_alert_observation(self) -> None:
        async def run() -> None:
            now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
            states = [
                {
                    "entity_id": "sensor.nevera_temperatura",
                    "state": "unavailable",
                    "last_updated": (now - dt.timedelta(minutes=5)).isoformat(),
                    "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"},
                }
            ]
            memory = FakeMemory()
            agent = MonitorTemperaturaAgent()
            context = AgentContext(now=now, services={"ha_client": FakeHAClient(states), "memory": memory})
            first = await agent.run(context)
            second = await agent.run(context)
            self.assertTrue(first.ok)
            self.assertTrue(second.data["deduplicated"])
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_missing_homeassistant_rest_returns_warning(self) -> None:
        async def run() -> None:
            agent = MonitorTemperaturaAgent()
            result = await agent.run(
                AgentContext(
                    now=dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC),
                    services={"segurai": FakeSegurAIWithoutHA()},
                )
            )
            self.assertFalse(result.ok)
            self.assertIn("homeassistant_unavailable", result.data["warnings"][0])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
