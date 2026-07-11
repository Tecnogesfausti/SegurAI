from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult


@dataclass
class AgentStats:
    runs: int = 0
    failures: int = 0
    last_started_at: str | None = None
    last_stopped_at: str | None = None
    last_run_at: str | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None
    last_message: str | None = None


@dataclass
class ManagedAgent:
    agent: Agent
    module_path: Path
    active: bool = False
    stats: AgentStats = field(default_factory=AgentStats)


class AgentManager:
    def __init__(
        self,
        agents_dir: Path,
        context_services: dict[str, Any] | None = None,
        event_logger: Callable[..., None] | None = None,
    ) -> None:
        self.agents_dir = agents_dir
        self.context_services = context_services or {}
        self.event_logger = event_logger
        self.agents: dict[str, ManagedAgent] = {}
        self.load_errors: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def discover(self) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.load_errors.clear()
        for path in sorted(self.agents_dir.glob("*.py")):
            if path.name in {"__init__.py", "base.py", "manager.py"}:
                continue
            try:
                self._load_module(path)
            except Exception:  # noqa: BLE001
                self.load_errors[str(path)] = traceback.format_exc(limit=5)
                self._emit("error", "agent_load_failed", path=str(path), error=self.load_errors[str(path)])

    def _load_module(self, path: Path) -> None:
        module_name = f"segurai_dynamic_agent_{path.stem}_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"No se pudo crear spec para {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        loaded = False
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Agent or not issubclass(obj, Agent):
                continue
            instance = obj()
            self._validate_agent(instance, path)
            self.agents[instance.name] = ManagedAgent(agent=instance, module_path=path)
            self._emit("info", "agent_loaded", agent=instance.name, path=str(path))
            loaded = True
        if not loaded:
            raise ValueError(f"{path} no declara ninguna subclase de Agent")

    def _validate_agent(self, agent: Agent, path: Path) -> None:
        metadata = getattr(agent, "metadata", None)
        if not isinstance(metadata, AgentMetadata):
            raise TypeError(f"{path} no define metadata AgentMetadata valida")
        if not metadata.name:
            raise ValueError(f"{path} define un agente sin nombre")
        if metadata.frequency_seconds < 1:
            raise ValueError(f"{metadata.name}: frequency_seconds debe ser >= 1")
        if metadata.daily_llm_budget_tokens < 0:
            raise ValueError(f"{metadata.name}: daily_llm_budget_tokens no puede ser negativo")

    async def start(self, name: str) -> bool:
        async with self._lock:
            managed = self.agents.get(name)
            if not managed:
                return False
            managed.active = True
            managed.stats.last_started_at = self._now()
            self._emit("info", "agent_started", agent=name)
            return True

    async def stop(self, name: str) -> bool:
        async with self._lock:
            managed = self.agents.get(name)
            if not managed:
                return False
            managed.active = False
            managed.stats.last_stopped_at = self._now()
            self._emit("info", "agent_stopped", agent=name)
            return True

    async def restart(self, name: str) -> bool:
        stopped = await self.stop(name)
        started = await self.start(name)
        return stopped and started

    async def run_once(self, name: str) -> AgentRunResult:
        managed = self.agents.get(name)
        if not managed:
            return AgentRunResult(ok=False, message=f"Agente no encontrado: {name}")

        context = AgentContext(now=self._datetime_now(), services=self.context_services)
        started = time.perf_counter()
        managed.stats.last_run_at = self._now()
        try:
            result = await managed.agent.run(context)
            managed.stats.runs += 1
            managed.stats.last_error = None if result.ok else result.message
            managed.stats.last_message = result.message
            self._emit(
                "info" if result.ok else "warn",
                "agent_run_finished",
                agent=name,
                ok=result.ok,
                result_message=result.message,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            managed.stats.failures += 1
            managed.stats.last_error = traceback.format_exc(limit=5)
            self._emit("error", "agent_run_failed", agent=name, error=managed.stats.last_error)
            return AgentRunResult(ok=False, message=f"{exc.__class__.__name__}: {exc}")
        finally:
            managed.stats.last_duration_ms = int((time.perf_counter() - started) * 1000)

    def list_agents(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name, managed in sorted(self.agents.items()):
            meta = managed.agent.metadata
            rows.append(
                {
                    "name": name,
                    "description": meta.description,
                    "active": managed.active,
                    "priority": meta.priority,
                    "entities": list(meta.entities),
                    "wake_events": list(meta.wake_events),
                    "frequency_seconds": meta.frequency_seconds,
                    "daily_llm_budget_tokens": meta.daily_llm_budget_tokens,
                    "recommended_model": meta.recommended_model,
                    "module_path": str(managed.module_path),
                    "stats": vars(managed.stats),
                }
            )
        return rows

    def active_agents(self) -> list[str]:
        return [name for name, managed in self.agents.items() if managed.active]

    def _now(self) -> str:
        return self._datetime_now().isoformat(timespec="seconds")

    def _datetime_now(self):
        import datetime as dt

        return dt.datetime.now(dt.UTC)

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if self.event_logger is None:
            return
        with contextlib.suppress(Exception):
            self.event_logger(level, "agent_manager", event, **fields)
