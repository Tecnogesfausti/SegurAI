from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
from typing import Any

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult
from agents.monitor_temperatura import HomeAssistantRestReader, parse_csv, parse_datetime


class DeviceHealthConfig:
    def __init__(
        self,
        *,
        allowlist: tuple[str, ...],
        ignorelist: tuple[str, ...],
        stale_minutes: int,
        very_stale_minutes: int,
        low_battery_percent: float,
    ) -> None:
        self.allowlist = allowlist
        self.ignorelist = ignorelist
        self.stale_minutes = stale_minutes
        self.very_stale_minutes = very_stale_minutes
        self.low_battery_percent = low_battery_percent

    @classmethod
    def from_env(cls) -> "DeviceHealthConfig":
        return cls(
            allowlist=parse_csv(os.getenv("MONITOR_DISPOSITIVOS_ENTITIES", "sensor.*,binary_sensor.*,switch.*,light.*,climate.*,cover.*,lock.*,alarm_control_panel.*")),
            ignorelist=parse_csv(os.getenv("MONITOR_DISPOSITIVOS_IGNORE", "")),
            stale_minutes=int(os.getenv("MONITOR_DISPOSITIVOS_STALE_MINUTES", "120")),
            very_stale_minutes=int(os.getenv("MONITOR_DISPOSITIVOS_VERY_STALE_MINUTES", "720")),
            low_battery_percent=float(os.getenv("MONITOR_DISPOSITIVOS_LOW_BATTERY_PERCENT", "20")),
        )


class MonitorDispositivosCaidosAgent(Agent):
    metadata = AgentMetadata(
        name="monitor_dispositivos_caidos",
        description=(
            "Detecta entidades de Home Assistant sin actualizar, estados unavailable/unknown, "
            "baterias bajas y fallos agrupados por dominio."
        ),
        priority=65,
        entities=("sensor.*", "binary_sensor.*", "switch.*", "light.*"),
        wake_events=("state_changed", "time_pattern"),
        frequency_seconds=600,
        daily_llm_budget_tokens=300,
        recommended_model="openai/gpt-4.1-nano",
    )

    def __init__(self) -> None:
        self._last_fingerprint: str | None = None

    async def run(self, context: AgentContext) -> AgentRunResult:
        config = DeviceHealthConfig.from_env()
        memory = context.services.get("memory")
        try:
            ha_client = self._resolve_ha_client(context)
            states = await ha_client.get_states()
        except Exception as exc:  # noqa: BLE001
            warning = f"homeassistant_unavailable:{exc.__class__.__name__}: {exc}"
            return AgentRunResult(
                ok=False,
                message="No puedo revisar salud de dispositivos de Home Assistant.",
                data={"warnings": [warning], "entities": []},
            )

        candidates = select_device_entities(states, config)
        analyses = [analyse_device_state(state, now=context.now, config=config) for state in candidates]
        alerts = [item for item in analyses if item["severity"] != "ok"]
        grouped = group_alerts(alerts)
        fingerprint = build_fingerprint(alerts)
        deduplicated = bool(fingerprint and fingerprint == self._last_fingerprint)
        if fingerprint:
            self._last_fingerprint = fingerprint
        elif self._last_fingerprint:
            self._last_fingerprint = None

        message = build_message(analyses, alerts, grouped, deduplicated=deduplicated)
        data = {
            "entities_checked": len(analyses),
            "alerts": alerts,
            "groups": grouped,
            "warnings": [],
            "deduplicated": deduplicated,
            "action_proposal": build_action_proposal(alerts),
            "config": {
                "allowlist": list(config.allowlist),
                "ignorelist": list(config.ignorelist),
                "stale_minutes": config.stale_minutes,
                "very_stale_minutes": config.very_stale_minutes,
                "low_battery_percent": config.low_battery_percent,
            },
        }
        result = AgentRunResult(ok=not any(item["severity"] == "critical" for item in alerts), message=message, data=data)
        if not deduplicated:
            self._save_observation(memory, result)
        return result

    def _resolve_ha_client(self, context: AgentContext) -> Any:
        injected = context.services.get("ha_client")
        if injected is not None:
            return injected
        segurai = context.services.get("segurai")
        if segurai is None:
            raise RuntimeError("missing_segurai_service")
        if not getattr(segurai, "has_homeassistant_rest", False):
            raise RuntimeError("missing_homeassistant_rest")
        return HomeAssistantRestReader(segurai)

    def _save_observation(self, memory: Any, result: AgentRunResult) -> None:
        if memory is not None and hasattr(memory, "add_observation"):
            memory.add_observation(
                source=self.metadata.name,
                summary=result.message,
                raw=json.dumps(result.data, ensure_ascii=False, default=str),
            )


def select_device_entities(states: list[dict[str, Any]], config: DeviceHealthConfig) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for state in states:
        entity_id = str(state.get("entity_id") or "")
        if not entity_id or any(fnmatch.fnmatch(entity_id, pattern) for pattern in config.ignorelist):
            continue
        if config.allowlist and not any(fnmatch.fnmatch(entity_id, pattern) for pattern in config.allowlist):
            continue
        selected.append(state)
    return selected


def analyse_device_state(state: dict[str, Any], *, now: dt.datetime, config: DeviceHealthConfig) -> dict[str, Any]:
    entity_id = str(state.get("entity_id") or "")
    attrs = state.get("attributes") or {}
    raw_state = str(state.get("state") or "")
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
    last_updated = parse_datetime(state.get("last_updated") or state.get("last_changed"))
    age_minutes = ((now - last_updated).total_seconds() / 60) if last_updated else None
    issues: list[dict[str, Any]] = []
    severity = "ok"

    if raw_state.lower() in {"unavailable", "unknown"}:
        severity = max_severity(severity, "critical" if raw_state.lower() == "unavailable" else "warning")
        issues.append({"type": raw_state.lower(), "message": f"estado {raw_state}"})

    if age_minutes is None:
        severity = max_severity(severity, "warning")
        issues.append({"type": "missing_timestamp", "message": "sin timestamp de actualizacion"})
    elif age_minutes >= config.very_stale_minutes:
        severity = max_severity(severity, "critical")
        issues.append({"type": "very_stale", "message": f"sin actualizar desde hace {age_minutes:.0f} minutos"})
    elif age_minutes >= config.stale_minutes and raw_state.lower() not in {"unavailable", "unknown"}:
        severity = max_severity(severity, "warning")
        issues.append({"type": "stale", "message": f"sin actualizar desde hace {age_minutes:.0f} minutos"})

    battery = battery_percent(entity_id, raw_state, attrs)
    if battery is not None and battery <= config.low_battery_percent:
        severity = max_severity(severity, "warning")
        issues.append({"type": "low_battery", "message": f"bateria al {battery:.0f}%"})

    return {
        "entity_id": entity_id,
        "domain": domain,
        "friendly_name": attrs.get("friendly_name"),
        "state": raw_state,
        "last_updated": last_updated.isoformat() if last_updated else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "battery_percent": battery,
        "severity": severity,
        "issues": issues,
    }


def battery_percent(entity_id: str, raw_state: str, attrs: dict[str, Any]) -> float | None:
    device_class = str(attrs.get("device_class") or "").lower()
    unit = str(attrs.get("unit_of_measurement") or "").strip()
    looks_like_battery = device_class == "battery" or "battery" in entity_id.lower() or "bateria" in entity_id.lower()
    if not looks_like_battery or unit != "%":
        return None
    try:
        return float(raw_state.replace(",", "."))
    except ValueError:
        return None


def group_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for alert in alerts:
        domain = str(alert.get("domain") or "unknown")
        group = groups.setdefault(domain, {"count": 0, "critical": 0, "warning": 0, "entities": []})
        group["count"] += 1
        group[str(alert.get("severity"))] += 1
        group["entities"].append(alert.get("entity_id"))
    return groups


def build_action_proposal(alerts: list[dict[str, Any]]) -> dict[str, Any] | None:
    critical = [item for item in alerts if item["severity"] == "critical"]
    if not critical:
        return None
    return {
        "type": "create_maintenance_task",
        "target": ",".join(str(item["entity_id"]) for item in critical[:10]),
        "reason": f"{len(critical)} entidades criticas en Home Assistant",
        "requires_confirmation": True,
        "risk_level": 2,
    }


def build_fingerprint(alerts: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for alert in sorted(alerts, key=lambda item: str(item.get("entity_id"))):
        issue_types = ",".join(sorted(str(issue.get("type")) for issue in alert.get("issues", [])))
        parts.append(f"{alert.get('entity_id')}:{alert.get('severity')}:{issue_types}")
    return "|".join(parts)


def build_message(
    analyses: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    grouped: dict[str, Any],
    *,
    deduplicated: bool,
) -> str:
    if not analyses:
        return "No hay entidades de Home Assistant que coincidan con el monitor de dispositivos."
    if not alerts:
        return f"Dispositivos: {len(analyses)} entidades revisadas, sin incidencias."
    critical = sum(1 for item in alerts if item["severity"] == "critical")
    warning = len(alerts) - critical
    top_domains = ", ".join(f"{domain}:{data['count']}" for domain, data in sorted(grouped.items())[:4])
    suffix = " Incidencia repetida, no se guarda nueva observacion." if deduplicated else ""
    return (
        f"Dispositivos: {len(alerts)} incidencias en {len(analyses)} entidades "
        f"({critical} criticas, {warning} avisos). Grupos: {top_domains}.{suffix}"
    )


def max_severity(current: str, candidate: str) -> str:
    order = {"ok": 0, "warning": 1, "critical": 2}
    return candidate if order[candidate] > order[current] else current
