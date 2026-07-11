from __future__ import annotations

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult


class MonitorTemperaturaAgent(Agent):
    metadata = AgentMetadata(
        name="monitor_temperatura",
        description="Monitoriza sensores de temperatura declarados y detecta lecturas no disponibles.",
        priority=50,
        entities=("sensor.wrover_temperatura",),
        wake_events=("state_changed",),
        frequency_seconds=1800,
        daily_llm_budget_tokens=2000,
        recommended_model="openai/gpt-4.1-nano",
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(
            ok=True,
            message="Fase 1: agente cargado correctamente. La ejecucion programada se implementara en Fase 2.",
            data={"entities": list(self.entities), "now": context.now.isoformat()},
        )

