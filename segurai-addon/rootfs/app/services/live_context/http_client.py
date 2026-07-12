from __future__ import annotations

import asyncio
from typing import Any

import httpx


class LiveContextHTTPClient:
    def __init__(self, *, timeout_seconds: float = 15, max_retries: int = 2, client: httpx.AsyncClient | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self._client = client

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self._client is not None:
                    response = await self._client.get(url, params=params)
                else:
                    async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                        response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
        assert last_error is not None
        raise last_error
