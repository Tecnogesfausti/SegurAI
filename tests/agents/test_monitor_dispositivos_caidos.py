from __future__ import annotations

import asyncio
import datetime as dt
import os
import unittest

from agents.base import AgentContext
from agents.monitor_dispositivos_caidos import MonitorDispositivosCaidosAgent


class FakeHAClient:
    def __init__(self, states) -> None:
        self.states = states

    async def get_states(self):
        return self.states


class FakeMemory:
    def __init__(self) -> None:
        self.observations = []

    def add_observation(self, **kwargs) -> None:
        self.observations.append(kwargs)


class FakeSegurAIWithoutHA:
    has_homeassistant_rest = False


class MonitorDispositivosCaidosAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = {key: os.environ.get(key) for key in os.environ if key.startswith("MONITOR_DISPOSITIVOS_")}
        for key in list(os.environ):
            if key.startswith("MONITOR_DISPOSITIVOS_"):
                os.environ.pop(key)

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("MONITOR_DISPOSITIVOS_"):
                os.environ.pop(key)
        for key, value in self.old_env.items():
            if value is not None:
                os.environ[key] = value

    def test_detects_unavailable_stale_and_low_battery(self) -> None:
        async def run() -> None:
            now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
            states = [
                {
                    "entity_id": "sensor.garaje_temperatura",
                    "state": "unavailable",
                    "last_updated": (now - dt.timedelta(minutes=5)).isoformat(),
                    "attributes": {"friendly_name": "Garaje temperatura"},
                },
                {
                    "entity_id": "binary_sensor.puerta_cocina",
                    "state": "off",
                    "last_updated": (now - dt.timedelta(hours=13)).isoformat(),
                    "attributes": {"friendly_name": "Puerta cocina"},
                },
                {
                    "entity_id": "sensor.mando_battery",
                    "state": "12",
                    "last_updated": (now - dt.timedelta(minutes=1)).isoformat(),
                    "attributes": {"device_class": "battery", "unit_of_measurement": "%"},
                },
            ]
            memory = FakeMemory()
            agent = MonitorDispositivosCaidosAgent()
            result = await agent.run(AgentContext(now=now, services={"ha_client": FakeHAClient(states), "memory": memory}))
            self.assertFalse(result.ok)
            self.assertEqual(result.data["entities_checked"], 3)
            issue_types = {issue["type"] for alert in result.data["alerts"] for issue in alert["issues"]}
            self.assertIn("unavailable", issue_types)
            self.assertIn("very_stale", issue_types)
            self.assertIn("low_battery", issue_types)
            self.assertEqual(result.data["action_proposal"]["requires_confirmation"], True)
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_deduplicates_repeated_device_alerts(self) -> None:
        async def run() -> None:
            now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
            states = [
                {
                    "entity_id": "light.patio",
                    "state": "unavailable",
                    "last_updated": now.isoformat(),
                    "attributes": {},
                }
            ]
            memory = FakeMemory()
            agent = MonitorDispositivosCaidosAgent()
            context = AgentContext(now=now, services={"ha_client": FakeHAClient(states), "memory": memory})
            first = await agent.run(context)
            second = await agent.run(context)
            self.assertFalse(first.ok)
            self.assertTrue(second.data["deduplicated"])
            self.assertEqual(len(memory.observations), 1)

        asyncio.run(run())

    def test_ignorelist_excludes_noisy_entities(self) -> None:
        async def run() -> None:
            os.environ["MONITOR_DISPOSITIVOS_IGNORE"] = "sensor.ruidoso"
            now = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.UTC)
            states = [
                {
                    "entity_id": "sensor.ruidoso",
                    "state": "unavailable",
                    "last_updated": now.isoformat(),
                    "attributes": {},
                }
            ]
            result = await MonitorDispositivosCaidosAgent().run(
                AgentContext(now=now, services={"ha_client": FakeHAClient(states)})
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["entities_checked"], 0)

        asyncio.run(run())

    def test_missing_homeassistant_rest_returns_warning(self) -> None:
        async def run() -> None:
            result = await MonitorDispositivosCaidosAgent().run(
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
