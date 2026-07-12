from __future__ import annotations

from abc import ABC, abstractmethod

from services.live_context.config import LiveContextConfig


class LiveContextProvider(ABC):
    domain: str
    source: str
    ttl_seconds: int

    @abstractmethod
    async def fetch(self, config: LiveContextConfig) -> dict:
        """Fetch and normalize provider data."""
