from __future__ import annotations

import json
import os
from typing import Any

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult
from agents.monitor_temperatura import parse_csv
from services.live_context.providers.traffic_dgt import normalize_road


class TrafficMonitorConfig:
    def __init__(self, *, roads: tuple[str, ...], min_severity: str) -> None:
        self.roads = tuple(road for road in (normalize_road(item) for item in roads) if road)
        self.min_severity = min_severity

    @classmethod
    def from_env(cls) -> "TrafficMonitorConfig":
        return cls(
            roads=parse_csv(os.getenv("MONITOR_TRAFICO_ROADS", "A-7,AP-7,V-30,CV-36,CV-33,N-340")),
            min_severity=os.getenv("MONITOR_TRAFICO_MIN_SEVERITY", "info").lower(),
        )


class MonitorTraficoAgent(Agent):
    metadata = AgentMetadata(
        name="monitor_trafico",
        description="Vigila incidencias de trafico cerca de Torrent, con foco en A-7 y rutas frecuentes.",
        priority=80,
        entities=(),
        wake_events=("time_pattern",),
        frequency_seconds=300,
        daily_llm_budget_tokens=500,
        recommended_model="openai/gpt-4.1-nano",
    )

    def __init__(self) -> None:
        self._last_fingerprint: str | None = None

    async def run(self, context: AgentContext) -> AgentRunResult:
        live_context = context.services.get("live_context")
        memory = context.services.get("memory")
        config = TrafficMonitorConfig.from_env()
        if live_context is None:
            return AgentRunResult(
                ok=False,
                message="LiveContextManager no esta disponible en context.services.",
                data={"warnings": ["missing_live_context"], "alerts": []},
            )

        result = await live_context.get_context(domains=["traffic"], max_items_per_domain=20)
        warnings = list(result.get("warnings", []))
        traffic = result.get("traffic") or {}
        warnings.extend(traffic.get("warnings") or [])
        incidents = traffic.get("data", {}).get("incidents", []) if isinstance(traffic.get("data"), dict) else []
        relevant = filter_relevant_incidents(incidents, config)
        fingerprint = build_fingerprint(relevant)
        deduplicated = bool(fingerprint and fingerprint == self._last_fingerprint)
        if fingerprint:
            self._last_fingerprint = fingerprint
        elif self._last_fingerprint:
            self._last_fingerprint = None

        message = build_message(relevant, warnings, config, deduplicated=deduplicated)
        data = {
            "traffic": traffic,
            "alerts": relevant,
            "warnings": warnings,
            "deduplicated": deduplicated,
            "watched_roads": list(config.roads),
            "action_proposal": build_action_proposal(relevant),
        }
        result_obj = AgentRunResult(ok=not any(item.get("severity") == "critical" for item in relevant), message=message, data=data)
        if not deduplicated:
            self._save_observation(memory, result_obj)
        return result_obj

    def _save_observation(self, memory: Any, result: AgentRunResult) -> None:
        if memory is not None and hasattr(memory, "add_observation"):
            memory.add_observation(
                source=self.metadata.name,
                summary=result.message,
                raw=json.dumps(result.data, ensure_ascii=False, default=str),
            )


def filter_relevant_incidents(incidents: list[dict[str, Any]], config: TrafficMonitorConfig) -> list[dict[str, Any]]:
    relevant: list[dict[str, Any]] = []
    min_rank = severity_rank(config.min_severity)
    for incident in incidents:
        road = normalize_road(str(incident.get("road") or incident.get("raw_road") or ""))
        severity = str(incident.get("severity") or "info").lower()
        if severity_rank(severity) < min_rank:
            continue
        if config.roads and road not in config.roads:
            continue
        item = dict(incident)
        item["road"] = road
        item["severity"] = severity if severity in {"info", "warning", "critical"} else "info"
        relevant.append(item)
    relevant.sort(key=lambda item: (severity_rank(str(item.get("severity"))), -(item.get("distance_km") or 9999)), reverse=True)
    return relevant


def build_action_proposal(incidents: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not incidents:
        return None
    first = incidents[0]
    return {
        "type": "check_alternative_route",
        "target": first.get("road") or "ruta_frecuente",
        "reason": first.get("description") or first.get("title") or "Incidencia de trafico en ruta vigilada",
        "requires_confirmation": True,
        "risk_level": 1,
    }


def build_fingerprint(incidents: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in sorted(incidents, key=lambda row: str(row.get("id") or row.get("title"))):
        parts.append(f"{item.get('road')}:{item.get('severity')}:{item.get('id')}:{item.get('title')}")
    return "|".join(parts)


def build_message(
    incidents: list[dict[str, Any]],
    warnings: list[str],
    config: TrafficMonitorConfig,
    *,
    deduplicated: bool,
) -> str:
    if warnings and not incidents:
        return f"Trafico: sin datos fiables para {', '.join(config.roads)}. Avisos: {len(warnings)}."
    if not incidents:
        return f"Trafico: sin incidencias relevantes en {', '.join(config.roads)}."
    critical = sum(1 for item in incidents if item.get("severity") == "critical")
    warning = sum(1 for item in incidents if item.get("severity") == "warning")
    first = incidents[0]
    distance = f" a {first['distance_km']} km" if first.get("distance_km") is not None else ""
    suffix = " Incidencia repetida, no se guarda nueva observacion." if deduplicated else ""
    return (
        f"Trafico: {len(incidents)} incidencias en rutas vigiladas ({critical} criticas, {warning} avisos). "
        f"{first.get('road')}: {first.get('title')}{distance}.{suffix}"
    )


def severity_rank(value: str) -> int:
    return {"info": 0, "warning": 1, "critical": 2}.get(value.lower(), 0)
