from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
from typing import Any

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult


class TemperatureConfig:
    def __init__(
        self,
        *,
        allowlist: tuple[str, ...],
        min_c: float,
        max_c: float,
        stale_minutes: int,
        rapid_delta_c: float,
        rapid_window_minutes: int,
    ) -> None:
        self.allowlist = allowlist
        self.min_c = min_c
        self.max_c = max_c
        self.stale_minutes = stale_minutes
        self.rapid_delta_c = rapid_delta_c
        self.rapid_window_minutes = rapid_window_minutes

    @classmethod
    def from_env(cls) -> "TemperatureConfig":
        return cls(
            allowlist=parse_csv(os.getenv("MONITOR_TEMPERATURA_ENTITIES", "")),
            min_c=float(os.getenv("MONITOR_TEMPERATURA_MIN_C", "5")),
            max_c=float(os.getenv("MONITOR_TEMPERATURA_MAX_C", "35")),
            stale_minutes=int(os.getenv("MONITOR_TEMPERATURA_STALE_MINUTES", "60")),
            rapid_delta_c=float(os.getenv("MONITOR_TEMPERATURA_RAPID_DELTA_C", "4")),
            rapid_window_minutes=int(os.getenv("MONITOR_TEMPERATURA_RAPID_WINDOW_MINUTES", "30")),
        )


class MonitorTemperaturaAgent(Agent):
    metadata = AgentMetadata(
        name="monitor_temperatura",
        description=(
            "Vigila sensores de temperatura de Home Assistant y detecta valores fuera de rango, "
            "sensores sin actualizar, estados no disponibles y cambios rapidos."
        ),
        priority=70,
        entities=("sensor.*",),
        wake_events=("state_changed",),
        frequency_seconds=300,
        daily_llm_budget_tokens=300,
        recommended_model="openai/gpt-4.1-nano",
    )

    def __init__(self) -> None:
        self._last_fingerprint: str | None = None

    async def run(self, context: AgentContext) -> AgentRunResult:
        config = TemperatureConfig.from_env()
        memory = context.services.get("memory")
        warnings: list[str] = []
        try:
            ha_client = self._resolve_ha_client(context)
            states = await ha_client.get_states()
        except Exception as exc:  # noqa: BLE001
            warning = f"homeassistant_unavailable:{exc.__class__.__name__}: {exc}"
            return AgentRunResult(
                ok=False,
                message="No puedo leer sensores de temperatura de Home Assistant.",
                data={"warnings": [warning], "entities": []},
            )

        sensors = select_temperature_sensors(states, config.allowlist)
        if not sensors:
            message = "No he encontrado sensores de temperatura que coincidan con la configuracion."
            result = AgentRunResult(ok=True, message=message, data={"warnings": warnings, "entities": []})
            self._save_observation(memory, result)
            return result

        analyses: list[dict[str, Any]] = []
        for state in sensors:
            analysis = analyse_temperature_state(state, now=context.now, config=config)
            if analysis["value_c"] is not None:
                try:
                    history = await ha_client.get_history(
                        entity_id=analysis["entity_id"],
                        start_time=context.now - dt.timedelta(minutes=config.rapid_window_minutes),
                        end_time=context.now,
                    )
                    add_history_analysis(analysis, history, config)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"history_unavailable:{analysis['entity_id']}:{exc.__class__.__name__}")
            analyses.append(analysis)

        alerts = [item for item in analyses if item["severity"] != "ok"]
        fingerprint = build_fingerprint(alerts)
        deduplicated = bool(fingerprint and fingerprint == self._last_fingerprint)
        if fingerprint:
            self._last_fingerprint = fingerprint
        elif self._last_fingerprint:
            self._last_fingerprint = None

        message = build_message(analyses, alerts, deduplicated=deduplicated)
        data = {
            "entities": analyses,
            "alerts": alerts,
            "warnings": warnings,
            "deduplicated": deduplicated,
            "config": {
                "allowlist": list(config.allowlist),
                "min_c": config.min_c,
                "max_c": config.max_c,
                "stale_minutes": config.stale_minutes,
                "rapid_delta_c": config.rapid_delta_c,
                "rapid_window_minutes": config.rapid_window_minutes,
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


class HomeAssistantRestReader:
    def __init__(self, segurai: Any) -> None:
        self.base_url = str(segurai.ha_base_url).rstrip("/")
        self.token = segurai.ha_token
        self.httpx = segurai.httpx
        if self.httpx is None:
            raise RuntimeError("missing_httpx")

    async def get_states(self) -> list[dict[str, Any]]:
        async with self.httpx.AsyncClient(timeout=20) as http:
            response = await http.get(f"{self.base_url}/api/states", headers=self._headers())
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, list) else []

    async def get_history(self, *, entity_id: str, start_time: dt.datetime, end_time: dt.datetime) -> list[dict[str, Any]]:
        params = {
            "filter_entity_id": entity_id,
            "minimal_response": "1",
            "no_attributes": "1",
            "end_time": end_time.isoformat(),
        }
        async with self.httpx.AsyncClient(timeout=20) as http:
            response = await http.get(
                f"{self.base_url}/api/history/period/{start_time.isoformat()}",
                headers=self._headers(),
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        return flatten_history(payload)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def select_temperature_sensors(states: list[dict[str, Any]], allowlist: tuple[str, ...]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for state in states:
        entity_id = str(state.get("entity_id") or "")
        attrs = state.get("attributes") or {}
        if not entity_id.startswith("sensor."):
            continue
        if allowlist:
            if not any(fnmatch.fnmatch(entity_id, pattern) for pattern in allowlist):
                continue
        elif not is_temperature_sensor(attrs):
            continue
        selected.append(state)
    return selected


def is_temperature_sensor(attrs: dict[str, Any]) -> bool:
    device_class = str(attrs.get("device_class") or "").lower()
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    return device_class == "temperature" or unit in {"c", "°c", "ºc", "f", "°f", "ºf"}


def analyse_temperature_state(state: dict[str, Any], *, now: dt.datetime, config: TemperatureConfig) -> dict[str, Any]:
    entity_id = str(state.get("entity_id") or "")
    attrs = state.get("attributes") or {}
    raw_state = state.get("state")
    value = parse_temperature_c(raw_state, attrs.get("unit_of_measurement"))
    last_updated = parse_datetime(state.get("last_updated") or state.get("last_changed"))
    age_minutes = ((now - last_updated).total_seconds() / 60) if last_updated else None
    issues: list[dict[str, Any]] = []
    severity = "ok"

    if str(raw_state).lower() in {"unknown", "unavailable", "none", ""}:
        issues.append({"type": "unavailable", "message": "sensor sin lectura disponible"})
        severity = max_severity(severity, "warning")
    elif value is None:
        issues.append({"type": "invalid_value", "message": f"valor no numerico: {raw_state}"})
        severity = max_severity(severity, "warning")
    else:
        if value < config.min_c:
            issues.append({"type": "below_range", "message": f"{value:.1f} C por debajo de {config.min_c:.1f} C"})
            severity = max_severity(severity, "critical")
        if value > config.max_c:
            issues.append({"type": "above_range", "message": f"{value:.1f} C por encima de {config.max_c:.1f} C"})
            severity = max_severity(severity, "critical")

    if age_minutes is None:
        issues.append({"type": "missing_timestamp", "message": "sin timestamp de actualizacion"})
        severity = max_severity(severity, "warning")
    elif age_minutes >= config.stale_minutes:
        issues.append({"type": "stale", "message": f"sin actualizar desde hace {age_minutes:.0f} minutos"})
        severity = max_severity(severity, "warning")

    return {
        "entity_id": entity_id,
        "friendly_name": attrs.get("friendly_name"),
        "state": raw_state,
        "value_c": value,
        "unit": attrs.get("unit_of_measurement"),
        "last_updated": last_updated.isoformat() if last_updated else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "status": "ok" if value is not None and severity != "critical" else "problem",
        "severity": severity,
        "issues": issues,
    }


def add_history_analysis(analysis: dict[str, Any], history: list[dict[str, Any]], config: TemperatureConfig) -> None:
    values: list[tuple[dt.datetime | None, float]] = []
    for point in history:
        raw = point.get("state")
        if str(raw).lower() in {"unknown", "unavailable", "none", ""}:
            continue
        value = parse_temperature_c(raw, point.get("unit_of_measurement"))
        if value is not None:
            values.append((parse_datetime(point.get("last_updated") or point.get("last_changed")), value))
    analysis["history_points"] = len(values)
    if len(values) < 2:
        return
    first_value = values[0][1]
    last_value = values[-1][1]
    delta = last_value - first_value
    analysis["variation_c"] = round(delta, 2)
    if abs(delta) >= config.rapid_delta_c:
        direction = "subida" if delta > 0 else "bajada"
        analysis["issues"].append(
            {
                "type": "rapid_change",
                "message": f"{direction} rapida de {delta:+.1f} C en {config.rapid_window_minutes} minutos",
            }
        )
        analysis["severity"] = max_severity(str(analysis["severity"]), "warning")


def parse_temperature_c(value: Any, unit: Any = None) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    unit_text = str(unit or "").lower()
    if unit_text in {"f", "°f", "ºf"}:
        return (number - 32) * 5 / 9
    return number


def parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    except ValueError:
        return None


def flatten_history(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return rows
    for entity_rows in payload:
        if not isinstance(entity_rows, list):
            continue
        for row in entity_rows:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def build_fingerprint(alerts: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for alert in sorted(alerts, key=lambda item: str(item.get("entity_id"))):
        issue_types = ",".join(sorted(str(issue.get("type")) for issue in alert.get("issues", [])))
        parts.append(f"{alert.get('entity_id')}:{alert.get('severity')}:{issue_types}")
    return "|".join(parts)


def build_message(analyses: list[dict[str, Any]], alerts: list[dict[str, Any]], *, deduplicated: bool) -> str:
    if not analyses:
        return "No hay sensores de temperatura para analizar."
    if not alerts:
        return f"Temperatura: {len(analyses)} sensores revisados, sin alertas."
    critical = sum(1 for item in alerts if item["severity"] == "critical")
    warning = len(alerts) - critical
    first = alerts[0]
    issue = first["issues"][0]["message"] if first.get("issues") else "incidencia"
    suffix = " Alerta repetida, no se guarda nueva observacion." if deduplicated else ""
    return (
        f"Temperatura: {len(alerts)} alertas en {len(analyses)} sensores "
        f"({critical} criticas, {warning} avisos). {first['entity_id']}: {issue}.{suffix}"
    )


def max_severity(current: str, candidate: str) -> str:
    order = {"ok": 0, "warning": 1, "critical": 2}
    return candidate if order[candidate] > order[current] else current


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
