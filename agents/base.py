from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentMetadata:
    name: str
    description: str
    priority: int = 50
    entities: tuple[str, ...] = ()
    wake_events: tuple[str, ...] = ()
    frequency_seconds: int = 3600
    daily_llm_budget_tokens: int = 0
    recommended_model: str | None = None


@dataclass
class AgentContext:
    now: dt.datetime
    services: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class Agent(ABC):
    metadata: AgentMetadata

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def priority(self) -> int:
        return self.metadata.priority

    @property
    def entities(self) -> tuple[str, ...]:
        return self.metadata.entities

    @property
    def frequency_seconds(self) -> int:
        return self.metadata.frequency_seconds

    @abstractmethod
    async def run(self, context: AgentContext) -> AgentRunResult:
        """Execute one monitoring pass."""

