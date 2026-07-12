from __future__ import annotations

import json
from typing import Any

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult


class MonitorClimaExteriorAgent(Agent):
    metadata = AgentMetadata(
        name="monitor_clima_exterior",
        description=(
            "Consulta clima exterior para Torrent y prepara recomendaciones de ventilacion, "
            "persianas, riego y actividad exterior."
        ),
        priority=75,
        entities=("sensor.temperatura_exterior", "sensor.humedad_exterior"),
        wake_events=("state_changed", "time_pattern"),
        frequency_seconds=900,
        daily_llm_budget_tokens=800,
        recommended_model="openai/gpt-4.1-nano",
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        live_context = context.services.get("live_context")
        memory = context.services.get("memory")
        warnings: list[str] = []
        if live_context is None:
            return AgentRunResult(
                ok=False,
                message="LiveContextManager no esta disponible en context.services.",
                data={"warnings": ["missing_live_context"]},
            )

        result = await live_context.get_context(
            domains=["weather"],
            max_items_per_domain=10,
        )
        warnings.extend(result.get("warnings", []))
        weather = result.get("weather", {})
        message, severity, recommendations = self._build_summary(weather)
        if memory is not None and hasattr(memory, "add_observation"):
            memory.add_observation(
                source=self.metadata.name,
                summary=message,
                raw=json.dumps({"weather": weather, "recommendations": recommendations}, ensure_ascii=False),
            )

        return AgentRunResult(
            ok=not bool(warnings),
            message=message,
            data={
                "weather": weather,
                "severity": severity,
                "recommendations": recommendations,
                "warnings": warnings,
            },
        )

    def _build_summary(self, weather: dict[str, Any]) -> tuple[str, str, list[str]]:
        data = weather.get("data") or {}
        current = data.get("current") or {}
        hourly = data.get("hourly_preview") or []
        warnings = weather.get("warnings") or []
        recommendations: list[str] = []
        severity = "bajo"

        temperature = as_float(current.get("temperature_2m"))
        humidity = as_float(current.get("relative_humidity_2m"))
        wind_gust = as_float(current.get("wind_gusts_10m"))
        rain_now = as_float(current.get("rain")) or as_float(current.get("precipitation")) or 0.0
        max_rain_probability = max(
            [as_float(row.get("precipitation_probability")) or 0.0 for row in hourly],
            default=0.0,
        )

        if temperature is not None and temperature >= 32:
            severity = "medio"
            recommendations.append("Evitar ventilacion en horas de mas calor y priorizar sombra/persianas.")
        if temperature is not None and temperature >= 38:
            severity = "alto"
            recommendations.append("Calor intenso: reducir actividad exterior y vigilar huerto/mascotas.")
        if max_rain_probability >= 60 or rain_now > 0:
            severity = max_severity(severity, "medio")
            recommendations.append("Probable lluvia: evitar riego automatico y revisar ventanas/tendidos.")
        if wind_gust is not None and wind_gust >= 50:
            severity = max_severity(severity, "alto")
            recommendations.append("Rachas fuertes: asegurar objetos exteriores y revisar persianas/toldos.")
        if humidity is not None and humidity >= 80 and temperature is not None and temperature >= 28:
            severity = max_severity(severity, "medio")
            recommendations.append("Bochorno alto: la ventilacion puede ser poco eficaz.")
        if not recommendations:
            recommendations.append("Sin acciones recomendadas ahora mismo.")

        parts = [weather.get("summary") or "Clima exterior actualizado."]
        if warnings:
            parts.append(f"Avisos de proveedor: {len(warnings)}")
        parts.append(f"Riesgo {severity}.")
        message = " ".join(parts)
        return message, severity, recommendations


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def max_severity(current: str, candidate: str) -> str:
    order = {"bajo": 0, "medio": 1, "alto": 2}
    return candidate if order[candidate] > order[current] else current
