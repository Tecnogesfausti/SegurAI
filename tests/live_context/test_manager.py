from __future__ import annotations

import asyncio
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from services.live_context.config import LiveContextConfig
from services.live_context.manager import LiveContextManager
from services.live_context.models import NormalizedContext
from services.live_context.providers.base import LiveContextProvider


class FlakyProvider(LiveContextProvider):
    domain = "weather"
    source = "test"
    ttl_seconds = 60

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def fetch(self, config: LiveContextConfig) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        now = dt.datetime.now(ZoneInfo(config.location.timezone))
        return NormalizedContext(
            domain=self.domain,
            location=config.location,
            source=self.source,
            fetched_at=now,
            expires_at=now + dt.timedelta(seconds=self.ttl_seconds),
            is_stale=False,
            confidence=1.0,
            summary="ok",
            data={"calls": self.calls},
        ).as_dict()


class LiveContextManagerTest(unittest.TestCase):
    def test_cache_and_stale_fallback(self) -> None:
        async def run() -> None:
            provider = FlakyProvider()
            manager = LiveContextManager(providers=[provider])
            first = await manager.get_context(["weather"])
            second = await manager.get_context(["weather"])
            self.assertEqual(provider.calls, 1)
            self.assertEqual(first["weather"]["summary"], "ok")
            self.assertEqual(second["weather"]["summary"], "ok")
            provider.fail = True
            manager.cache._entries[manager._cache_key("weather")].expires_at = dt.datetime.now(ZoneInfo(manager.config.location.timezone))
            stale = await manager.get_context(["weather"])
            self.assertTrue(stale["weather"]["is_stale"])
            self.assertIn("weather: RuntimeError: boom", stale["warnings"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
