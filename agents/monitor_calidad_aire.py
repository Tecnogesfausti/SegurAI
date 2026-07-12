from __future__ import annotations

import json
import os
from typing import Any

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult


class AirQualityMonitorConfig:
    def __init__(self, *, max_exercise_aqi: float, max_ventilation_aqi: float) -> None:
        self.max_exercise_aqi = max_exercise_aqi
        self.max_ventilation_aqi = max_ventilation_aqi

    @classmethod
    def from_env(cls) -> "AirQualityMonitorConfig":
        return cls(
            max_exercise_aqi=float(os.getenv("MONITOR_CALIDAD_AIRE_MAX_EXERCISE_AQI", "60")),
            max_ventilation_aqi=float(os.getenv("MONITOR_CALIDAD_AIRE_MAX_VENTILATION_AQI", "80")),
        )


class MonitorCalidadAireAgent(Agent):
    metadata = AgentMetadata(
        name="monitor_calidad_aire",
        description="Vigila calidad del aire en Torrent y Valencia y recomienda prudencia para exterior.",
        priority=55,
        entities=(),
        wake_events=("time_pattern",),
        frequency_seconds=1800,
        daily_llm_budget_tokens=500,
        recommended_model="openai/gpt-4.1-nano",
    )

    def __init__(self) -> None:
        self._last_fingerprint: str | None = None

    async def run(self, context: AgentContext) -> AgentRunResult:
        live_context = context.services.get("live_context")
        memory = context.services.get("memory")
        config = AirQualityMonitorConfig.from_env()
        if live_context is None:
            return AgentRunResult(
                ok=False,
                message="LiveContextManager no esta disponible en context.services.",
                data={"warnings": ["missing_live_context"], "alerts": []},
            )

        result = await live_context.get_context(domains=["air_quality"], max_items_per_domain=10)
        warnings = list(result.get("warnings", []))
        air_quality = result.get("air_quality") or {}
        warnings.extend(air_quality.get("warnings") or [])
        locations = air_quality.get("data", {}).get("locations", []) if isinstance(air_quality.get("data"), dict) else []
        analyses = [analyse_location(row, config) for row in locations]
        alerts = [item for item in analyses if item["severity"] != "ok"]
        recommendations = build_recommendations(analyses, config)
        fingerprint = build_fingerprint(alerts)
        deduplicated = bool(fingerprint and fingerprint == self._last_fingerprint)
        if fingerprint:
            self._last_fingerprint = fingerprint
        elif self._last_fingerprint:
            self._last_fingerprint = None

        message = build_message(analyses, warnings, deduplicated=deduplicated)
        data = {
            "air_quality": air_quality,
            "locations": analyses,
            "alerts": alerts,
            "warnings": warnings,
            "recommendations": recommendations,
            "deduplicated": deduplicated,
            "config": {
                "max_exercise_aqi": config.max_exercise_aqi,
                "max_ventilation_aqi": config.max_ventilation_aqi,
            },
        }
        result_obj = AgentRunResult(ok=not any(item["severity"] == "critical" for item in alerts), message=message, data=data)
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


def analyse_location(row: dict[str, Any], config: AirQualityMonitorConfig) -> dict[str, Any]:
    aqi = as_float(row.get("european_aqi"))
    severity = severity_for_aqi(aqi)
    issues: list[dict[str, Any]] = []
    if aqi is None:
        issues.append({"type": "missing_aqi", "message": "sin indice europeo de calidad del aire"})
        severity = "warning"
    elif aqi > config.max_exercise_aqi:
        issues.append({"type": "exercise_caution", "message": f"AQI {aqi:.0f}: prudencia con ejercicio exterior"})
    if aqi is not None and aqi > config.max_ventilation_aqi:
        issues.append({"type": "avoid_ventilation", "message": f"AQI {aqi:.0f}: mejor evitar ventilacion prolongada"})
    return {
        "name": row.get("name"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "type": row.get("type", "forecast"),
        "european_aqi": aqi,
        "category": row.get("category"),
        "current": row.get("current") or {},
        "severity": severity,
        "issues": issues,
    }


def build_recommendations(analyses: list[dict[str, Any]], config: AirQualityMonitorConfig) -> list[str]:
    del config
    if not analyses:
        return ["Sin datos suficientes para recomendar actividad exterior."]
    worst = worst_location(analyses)
    aqi = worst.get("european_aqi")
    if aqi is None:
        return ["Consultar otra fuente antes de decidir actividad exterior."]
    if aqi > 100:
        return ["Evitar ejercicio exterior intenso y priorizar espacios interiores ventilados con filtrado si existe."]
    if aqi > 80:
        return ["Reducir ejercicio exterior prolongado y evitar ventilar en horas de peor calidad."]
    if aqi > 60:
        return ["Actividad exterior suave aceptable; prudencia si hay asma, alergias o sensibilidad respiratoria."]
    return ["Calidad del aire aceptable para actividad exterior normal."]


def build_message(analyses: list[dict[str, Any]], warnings: list[str], *, deduplicated: bool) -> str:
    if warnings and not analyses:
        return f"Calidad del aire: sin datos fiables. Avisos: {len(warnings)}."
    if not analyses:
        return "Calidad del aire: sin datos disponibles para Torrent o Valencia."
    worst = worst_location(analyses)
    aqi = worst.get("european_aqi")
    category = worst.get("category") or "sin_datos"
    parts = [f"{item.get('name')}: AQI {item.get('european_aqi')} ({item.get('category')})" for item in analyses]
    suffix = " Incidencia repetida, no se guarda nueva observacion." if deduplicated else ""
    return f"Calidad del aire: {'; '.join(parts)}. Peor punto: {worst.get('name')} AQI {aqi} ({category}).{suffix}"


def build_fingerprint(alerts: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in sorted(alerts, key=lambda row: str(row.get("name"))):
        issue_types = ",".join(sorted(str(issue.get("type")) for issue in item.get("issues", [])))
        parts.append(f"{item.get('name')}:{item.get('severity')}:{item.get('category')}:{issue_types}")
    return "|".join(parts)


def worst_location(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    return max(analyses, key=lambda row: row.get("european_aqi") if row.get("european_aqi") is not None else -1)


def severity_for_aqi(value: float | None) -> str:
    if value is None:
        return "warning"
    if value > 100:
        return "critical"
    if value > 60:
        return "warning"
    return "ok"


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
