#!/usr/bin/env python3
"""
SegurAI: agente de terminal 24/7 con MCP, OpenRouter/DeepSeek y memoria local.

Uso:
  python3 segurai.py
  python3 segurai.py --mcp-url http://supervisor/core/api/mcp
  python3 segurai.py -- npx -y <servidor-mcp-ha>

Comandos dentro del terminal:
  /help                 Muestra ayuda
  /memoria              Lista memorias guardadas
  /sensores             Lista observaciones recientes
  /aprender <texto>     Guarda una memoria manual
  /estado               Muestra modelo, MCP y tareas activas
  /salir                Cierra el agente
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import dataclasses
import datetime as dt
import json
import os
import re
import signal
import sqlite3
import sys
import textwrap
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agents.manager import AgentManager
from tools.registry import builtin_tool_names, builtin_tool_schemas, call_builtin_tool as call_indexed_builtin_tool
from services.live_context.manager import LiveContextManager

try:
    import readline
except ModuleNotFoundError:
    readline = None  # type: ignore[assignment]

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    import httpx
    from openai import AsyncOpenAI
    from dotenv import load_dotenv
    import yaml
except ModuleNotFoundError as exc:
    MISSING_DEPENDENCY = exc.name
    ClientSession = Any  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    httpx = None  # type: ignore[assignment]
    AsyncOpenAI = None  # type: ignore[assignment]
    load_dotenv = None  # type: ignore[assignment]
    yaml = None  # type: ignore[assignment]
else:
    MISSING_DEPENDENCY = None


APP_NAME = "SegurAI"
DEFAULT_MODEL = "openrouter/free"
FALLBACK_MODEL = "openrouter/free"
DEFAULT_DB = "segurai_memory.sqlite3"
DEFAULT_POLL_SECONDS = 300
DEFAULT_MODEL_ROUTES = os.getenv("SEGURAI_MODEL_ROUTES", "model_routes.yaml")
DEFAULT_AGENTS_DIR = os.getenv("SEGURAI_AGENTS_DIR", "agents")
MAX_TOOL_ROUNDS = 6
MAX_HISTORY_MESSAGES = 24
DEFAULT_TIMEZONE = os.getenv("SEGURAI_TIMEZONE", "Europe/Madrid")
TASK_TIMEOUT_SECONDS = int(os.getenv("SEGURAI_TASK_TIMEOUT_SECONDS", "45"))
DEFAULT_FS_ROOTS = os.getenv("SEGURAI_FS_ROOTS", str(Path.cwd()))
DEFAULT_LOG_FILE = os.getenv("SEGURAI_LOG_FILE", "segurai_runtime.log")
DEFAULT_HISTORY_FILE = os.getenv("SEGURAI_HISTORY_FILE", ".segurai_history")
DEFAULT_HISTORY_LIMIT = int(os.getenv("SEGURAI_HISTORY_LIMIT", "1000"))


def configure_runtime_defaults_from_env() -> None:
    global DEFAULT_TIMEZONE, TASK_TIMEOUT_SECONDS, DEFAULT_FS_ROOTS, DEFAULT_LOG_FILE, DEFAULT_HISTORY_FILE, DEFAULT_HISTORY_LIMIT

    DEFAULT_TIMEZONE = os.getenv("SEGURAI_TIMEZONE", DEFAULT_TIMEZONE)
    DEFAULT_FS_ROOTS = os.getenv("SEGURAI_FS_ROOTS", DEFAULT_FS_ROOTS)
    DEFAULT_LOG_FILE = os.getenv("SEGURAI_LOG_FILE", DEFAULT_LOG_FILE)
    DEFAULT_HISTORY_FILE = os.getenv("SEGURAI_HISTORY_FILE", DEFAULT_HISTORY_FILE)
    history_limit_text = os.getenv("SEGURAI_HISTORY_LIMIT")
    if history_limit_text:
        with contextlib.suppress(ValueError):
            DEFAULT_HISTORY_LIMIT = max(1, int(history_limit_text))
    timeout_text = os.getenv("SEGURAI_TASK_TIMEOUT_SECONDS")
    if timeout_text:
        try:
            TASK_TIMEOUT_SECONDS = max(1, int(timeout_text))
        except ValueError:
            runtime_log(
                "warn",
                "config",
                "SEGURAI_TASK_TIMEOUT_SECONDS invalido; se mantiene valor anterior",
                value=timeout_text,
            )


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def runtime_log(level: str, component: str, message: str, **fields: Any) -> None:
    payload = {
        "ts": utc_now(),
        "level": level,
        "component": component,
        "message": message,
        **fields,
    }
    path = Path(DEFAULT_LOG_FILE).expanduser()
    with contextlib.suppress(Exception):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def tail_file(path: Path, limit: int = 50) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max(1, min(limit, 500)) :]


def local_now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(DEFAULT_TIMEZONE))


def parse_datetime_to_utc(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds")


def parse_fs_roots(value: str) -> list[Path]:
    roots: list[Path] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        roots.append(Path(text).expanduser().resolve())
    return roots or [Path.cwd().resolve()]


async def fetch_openrouter_model_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    if httpx is None:
        return catalog
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            models = response.json().get("data", [])
    except Exception:
        return catalog
    for model in models:
        pricing = model.get("pricing") or {}
        model_id = model.get("id")
        if not model_id:
            continue
        try:
            input_price = float(pricing.get("prompt", 0))
            output_price = float(pricing.get("completion", 0))
        except (TypeError, ValueError):
            input_price = 0.0
            output_price = 0.0
        params = set(model.get("supported_parameters") or [])
        catalog[model_id] = {
            "id": model_id,
            "name": model.get("name"),
            "context_length": model.get("context_length"),
            "input_price": input_price,
            "output_price": output_price,
            "input_price_per_million": input_price * 1_000_000,
            "output_price_per_million": output_price * 1_000_000,
            "supports_tools": "tools" in params or "tool_choice" in params,
            "supports_structured_outputs": "structured_outputs" in params,
            "supported_parameters": sorted(params),
        }
    return catalog


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(str(content))
    return max(1, chars // 4)


@dataclasses.dataclass
class ModelRequest:
    task: str
    prompt_tokens_estimate: int
    requires_tools: bool = False
    requires_memory: bool = False
    priority: str = "cost"
    max_budget_usd: float | None = None
    preferred_model: str | None = None


@dataclasses.dataclass
class ModelSelection:
    model: str
    reason: str
    estimated_cost_usd: float | None
    fallbacks: list[str]


class ModelRouter:
    def __init__(self, config_path: Path, model_catalog: dict[str, dict[str, Any]]) -> None:
        self.config_path = config_path
        self.model_catalog = model_catalog
        self.config = self._load_config(config_path)

    def _load_config(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"default": DEFAULT_MODEL, "routes": {}}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if yaml is not None else None
        if not isinstance(data, dict):
            return {"default": DEFAULT_MODEL, "routes": {}}
        data.setdefault("routes", {})
        return data

    def select(self, request: ModelRequest) -> ModelSelection:
        routes = self.config.get("routes") or {}
        route = routes.get(request.task) or {}
        candidates = self._candidate_models(request, route)
        chosen = candidates[0]
        reason_parts = []
        if request.preferred_model:
            reason_parts.append(f"modelo preferido por usuario: {request.preferred_model}")
        elif route.get("model"):
            reason_parts.append(f"ruta '{request.task}' -> {route['model']}")
        else:
            reason_parts.append(f"default -> {chosen}")
        if route.get("reason"):
            reason_parts.append(str(route["reason"]))
        if request.requires_tools:
            reason_parts.append("requiere tools")
        if request.max_budget_usd is not None:
            reason_parts.append(f"presupuesto max ${request.max_budget_usd:.6f}")

        return ModelSelection(
            model=chosen,
            reason="; ".join(reason_parts),
            estimated_cost_usd=self.estimate_cost(chosen, request.prompt_tokens_estimate, 600),
            fallbacks=[model for model in candidates[1:] if model != chosen],
        )

    def _candidate_models(self, request: ModelRequest, route: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for model in (
            request.preferred_model,
            route.get("model"),
            *route.get("fallbacks", []),
            *self.config.get("fallbacks", []),
            self.config.get("default"),
            FALLBACK_MODEL,
        ):
            if isinstance(model, str) and model and model not in candidates:
                candidates.append(model)

        filtered = [model for model in candidates if self._allowed(model, request)]
        return filtered or candidates or [FALLBACK_MODEL]

    def _allowed(self, model: str, request: ModelRequest) -> bool:
        meta = self.model_catalog.get(model, {})
        if request.requires_tools and meta and not meta.get("supports_tools", False):
            return False
        if request.max_budget_usd is not None:
            estimated = self.estimate_cost(model, request.prompt_tokens_estimate, 600)
            if estimated is not None and estimated > request.max_budget_usd:
                return False
        priority = self.config.get("priorities", {}).get(request.priority, {})
        max_in = priority.get("max_input_price_per_million")
        max_out = priority.get("max_output_price_per_million")
        if meta and max_in is not None and meta.get("input_price_per_million", 0) > float(max_in):
            return False
        if meta and max_out is not None and meta.get("output_price_per_million", 0) > float(max_out):
            return False
        return True

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        meta = self.model_catalog.get(model)
        if not meta:
            return None
        return (input_tokens * float(meta["input_price"])) + (output_tokens * float(meta["output_price"]))

    def price_tuple(self, model: str) -> tuple[float, float] | None:
        meta = self.model_catalog.get(model)
        if not meta:
            return None
        return float(meta["input_price"]), float(meta["output_price"])

    def describe(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "default": self.config.get("default"),
            "routes": self.config.get("routes", {}),
            "fallbacks": self.config.get("fallbacks", []),
        }


def compact_json(value: Any, max_chars: int = 6000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncado]"
    return text


def safe_tool_name(name: str) -> str:
    fixed = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    if not re.match(r"^[a-zA-Z_]", fixed):
        fixed = f"tool_{fixed}"
    return fixed[:64]


def tool_result_to_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "model_dump"):
            parts.append(compact_json(item.model_dump()))
        else:
            parts.append(str(item))
    return "\n".join(parts).strip()


def exception_summary(exc: BaseException) -> str:
    if isinstance(exc, ExceptionGroup):
        messages: list[str] = []
        for sub_exc in exc.exceptions:
            messages.append(exception_summary(sub_exc))
        return "; ".join(message for message in messages if message) or str(exc)
    return f"{exc.__class__.__name__}: {exc}"


def derive_ha_base_url(mcp_url: str | None) -> str | None:
    if not mcp_url:
        return None
    for suffix in ("/api/mcp", "/mcp"):
        if mcp_url.endswith(suffix):
            return mcp_url[: -len(suffix)]
    return mcp_url.rstrip("/")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                summary TEXT NOT NULL,
                raw TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                raw TEXT
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                result TEXT,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                estimated_cost_usd REAL,
                context TEXT,
                provider TEXT,
                duration_ms INTEGER,
                router_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories(topic);
            CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_run_at ON tasks(status, run_at);
            CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
            """
        )
        self._ensure_usage_columns()
        self.conn.commit()

    def _ensure_usage_columns(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(usage_events)")
        }
        additions = {
            "provider": "TEXT",
            "duration_ms": "INTEGER",
            "router_reason": "TEXT",
        }
        for column, column_type in additions.items():
            if column not in columns:
                self.conn.execute(f"ALTER TABLE usage_events ADD COLUMN {column} {column_type}")

    def close(self) -> None:
        self.conn.close()

    def add_memory(
        self,
        *,
        kind: str,
        topic: str,
        content: str,
        confidence: float = 0.7,
        source: str,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO memories(created_at, updated_at, kind, topic, content, confidence, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, now, kind[:40], topic[:120], content.strip(), confidence, source[:80]),
        )
        self.conn.commit()

    def add_observation(self, *, source: str, summary: str, raw: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO observations(created_at, source, summary, raw) VALUES (?, ?, ?, ?)",
            (utc_now(), source[:80], summary.strip(), raw),
        )
        self.conn.commit()

    def add_event(self, *, level: str, message: str, raw: str | None = None) -> None:
        runtime_log(level, "memory.event", message, raw=raw)
        self.conn.execute(
            "INSERT INTO events(created_at, level, message, raw) VALUES (?, ?, ?, ?)",
            (utc_now(), level[:20], message.strip(), raw),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT created_at, level, message, raw
                FROM events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def add_usage_event(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        estimated_cost_usd: float | None,
        context: str,
        provider: str | None,
        duration_ms: int | None,
        router_reason: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO usage_events(
                created_at, model, prompt_tokens, completion_tokens, total_tokens,
                estimated_cost_usd, context, provider, duration_ms, router_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                context[:120],
                provider,
                duration_ms,
                router_reason[:1000] if router_reason else None,
            ),
        )
        self.conn.commit()

    def usage_summary(self, limit: int = 10) -> dict[str, Any]:
        total = self.conn.execute(
            """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(estimated_cost_usd), 0) AS cost
            FROM usage_events
            """
        ).fetchone()
        recent = list(
            self.conn.execute(
                """
                SELECT created_at, model, prompt_tokens, completion_tokens, total_tokens,
                       estimated_cost_usd, context, provider, duration_ms, router_reason
                FROM usage_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
        return {"total": dict(total), "recent": [dict(row) for row in recent]}

    def recent_memories(self, limit: int = 12) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, created_at, kind, topic, content, confidence, source
                FROM memories
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def search_memories(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) >= 3]
        if not terms:
            return self.recent_memories(limit)

        rows = list(
            self.conn.execute(
                """
                SELECT id, created_at, kind, topic, content, confidence, source
                FROM memories
                ORDER BY updated_at DESC, id DESC
                LIMIT 200
                """
            )
        )

        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            haystack = f"{row['topic']} {row['content']}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]] or self.recent_memories(limit)

    def recent_observations(self, limit: int = 8) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, created_at, source, summary
                FROM observations
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def add_task(self, *, run_at: str, title: str, instruction: str) -> int:
        now = utc_now()
        cursor = self.conn.execute(
            """
            INSERT INTO tasks(created_at, updated_at, run_at, status, title, instruction)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (now, now, parse_datetime_to_utc(run_at), title.strip()[:160], instruction.strip()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_tasks(self, *, include_done: bool = False, limit: int = 30) -> list[sqlite3.Row]:
        statuses = ("pending", "running", "failed") if not include_done else (
            "pending",
            "running",
            "failed",
            "done",
            "cancelled",
        )
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.conn.execute(
                f"""
                SELECT id, created_at, updated_at, run_at, status, title, instruction, result, last_error
                FROM tasks
                WHERE status IN ({placeholders})
                ORDER BY run_at ASC, id ASC
                LIMIT ?
                """,
                (*statuses, limit),
            )
        )

    def get_due_tasks(self, *, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, run_at, status, title, instruction
                FROM tasks
                WHERE status = 'pending' AND run_at <= ?
                ORDER BY run_at ASC, id ASC
                LIMIT ?
                """,
                (utc_now(), limit),
            )
        )

    def reset_running_tasks(self) -> None:
        self.conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, status = 'pending', last_error = 'Reiniciada tras arranque del agente'
            WHERE status = 'running'
            """,
            (utc_now(),),
        )
        self.conn.commit()

    def update_task(
        self,
        *,
        task_id: int,
        run_at: str | None = None,
        title: str | None = None,
        instruction: str | None = None,
        status: str | None = None,
    ) -> bool:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [utc_now()]
        if run_at is not None:
            fields.append("run_at = ?")
            values.append(parse_datetime_to_utc(run_at))
        if title is not None:
            fields.append("title = ?")
            values.append(title.strip()[:160])
        if instruction is not None:
            fields.append("instruction = ?")
            values.append(instruction.strip())
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        values.append(task_id)
        cursor = self.conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def cancel_task(self, task_id: int) -> bool:
        return self.update_task(task_id=task_id, status="cancelled")

    def mark_task_running(self, task_id: int) -> bool:
        return self.update_task(task_id=task_id, status="running")

    def mark_task_done(self, task_id: int, result: str) -> None:
        self.conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, status = 'done', result = ?, last_error = NULL
            WHERE id = ?
            """,
            (utc_now(), result.strip()[:4000], task_id),
        )
        self.conn.commit()

    def mark_task_failed(self, task_id: int, error: str) -> None:
        self.conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, status = 'failed', last_error = ?
            WHERE id = ?
            """,
            (utc_now(), error.strip()[:4000], task_id),
        )
        self.conn.commit()


@dataclasses.dataclass
class RuntimeConfig:
    db_path: Path
    model_routes_path: Path
    agents_dir: Path
    poll_seconds: int
    sensor_prompt: str
    enable_sensor_loop: bool
    require_action_confirmation: bool
    service_mode: bool
    mcp_url: str | None
    mcp_cmd: list[str]
    ha_base_url: str | None
    ha_token: str | None
    fs_roots: list[Path]


class SegurAIAgent:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        router: ModelRouter,
        session: ClientSession,
        memory: MemoryStore,
        tools: list[Any],
        require_action_confirmation: bool,
        ha_base_url: str | None,
        ha_token: str | None,
        fs_roots: list[Path],
    ) -> None:
        self.client = client
        self.router = router
        self.session = session
        self.memory = memory
        self.require_action_confirmation = require_action_confirmation
        self.ha_base_url = ha_base_url
        self.ha_token = ha_token
        self.fs_roots = fs_roots
        self.yaml = yaml
        self.httpx = httpx
        self.app_name = APP_NAME
        self.has_homeassistant_rest = bool(self.ha_base_url and self.ha_token)
        self.ask_lock = asyncio.Lock()
        self.messages: list[dict[str, Any]] = []
        self.tool_name_map: dict[str, str] = {}
        self.openai_tools = self._convert_tools(tools)
        self.tool_names = [t.name for t in tools]
        self.tool_names.extend(["schedule_task", "list_scheduled_tasks", "edit_scheduled_task", "cancel_scheduled_task"])
        self.tool_names.extend(builtin_tool_names(include_homeassistant=self.has_homeassistant_rest))

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        used: set[str] = set()
        for tool in tools:
            name = safe_tool_name(tool.name)
            while name in used:
                name = safe_tool_name(f"{name}_x")
            used.add(name)
            self.tool_name_map[name] = tool.name
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.description or f"Herramienta MCP {tool.name}",
                        "parameters": tool.inputSchema
                        or {"type": "object", "properties": {}, "additionalProperties": True},
                    },
                }
            )
        self.tool_name_map["schedule_task"] = "__builtin__:schedule_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "schedule_task",
                    "description": (
                        "Crea una tarea futura persistente. Para rangos de tiempo, crea una tarea de inicio "
                        "y otra de fin. La instrucción debe ser concreta y ejecutable."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "run_at": {
                                "type": "string",
                                "description": "Fecha/hora ISO 8601. Si no hay zona, se interpreta como Europe/Madrid.",
                            },
                            "title": {"type": "string", "description": "Título corto de la tarea."},
                            "instruction": {
                                "type": "string",
                                "description": "Qué debe ejecutar SegurAI cuando llegue la hora.",
                            },
                        },
                        "required": ["run_at", "title", "instruction"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["list_scheduled_tasks"] = "__builtin__:list_scheduled_tasks"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled_tasks",
                    "description": "Lista tareas programadas pendientes, fallidas o, si se pide, también completadas.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_done": {"type": "boolean", "default": False},
                            "limit": {"type": "integer", "default": 30},
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["edit_scheduled_task"] = "__builtin__:edit_scheduled_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "edit_scheduled_task",
                    "description": "Edita una tarea programada existente por id.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "run_at": {"type": "string"},
                            "title": {"type": "string"},
                            "instruction": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "cancelled"],
                                "description": "Usa pending para reactivar o cancelled para cancelar.",
                            },
                        },
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["cancel_scheduled_task"] = "__builtin__:cancel_scheduled_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled_task",
                    "description": "Cancela una tarea programada por id.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        for schema in builtin_tool_schemas(include_homeassistant=self.has_homeassistant_rest):
            name = schema["function"]["name"]
            self.tool_name_map[name] = f"__builtin__:{name}"
            converted.append(schema)
        return converted

    def _system_prompt(self, user_text: str | None = None) -> str:
        memories = self.memory.search_memories(user_text or "", limit=10)
        observations = self.memory.recent_observations(limit=6)
        pending_tasks = self.memory.list_tasks(include_done=False, limit=8)

        memory_block = "\n".join(
            f"- [{m['kind']}/{m['topic']}] {m['content']} (confianza {m['confidence']:.2f})"
            for m in memories
        ) or "- Sin memoria útil todavía."
        observation_block = "\n".join(
            f"- {o['created_at']} {o['source']}: {o['summary']}" for o in observations
        ) or "- Sin observaciones recientes."
        task_block = "\n".join(
            f"- #{t['id']} {t['run_at']} [{t['status']}] {t['title']}: {t['instruction']}"
            for t in pending_tasks
        ) or "- Sin tareas pendientes."

        confirmation_rule = (
            "No ejecutes acciones que cambien el estado de Home Assistant, seguridad, alarmas, "
            "cerraduras, sirenas, cámaras o automatizaciones sin pedir confirmación explícita."
            if self.require_action_confirmation
            else "Puedes ejecutar acciones si el usuario las pide de forma clara."
        )

        return f"""
Eres SegurAI, un agente local de terminal para Home Assistant.
Objetivo: ayudar al usuario, observar sensores, detectar patrones útiles y recordar preferencias/hechos.
Fecha/hora local actual: {local_now().isoformat(timespec="seconds")}.

Reglas:
- Usa herramientas MCP cuando necesites consultar Home Assistant o ejecutar una acción.
- Para preguntas históricas usa ha_get_history o ha_get_logbook si están disponibles.
- Para acciones futuras o diferidas usa schedule_task; no prometas recordar algo sin crear tarea.
- Para intervalos, crea dos tareas: una de inicio y otra de fin. Ejemplo: encender a las 23:00 y apagar a las 03:00 del día siguiente.
- Para editar o cancelar tareas usa edit_scheduled_task o cancel_scheduled_task.
- Para leer, contar, crear o borrar ficheros usa fs_list_dir, fs_read_file, fs_count_text, fs_write_file y fs_delete_path.
- Para sensores en ficheros usa sensor_read_file; para exportar datos usa data_write_file; para consultar páginas web públicas usa web_fetch_url.
- No borres ni sobrescribas ficheros salvo que el usuario lo pida de forma explícita.
- Solo puedes acceder a estas raíces de sistema de ficheros: {", ".join(str(root) for root in self.fs_roots)}.
- {confirmation_rule}
- Si un dato es incierto, dilo y consulta sensores si hace falta.
- Responde en español, directo y operativo.
- Cuando evalúes seguridad, incluye nivel de riesgo bajo/medio/alto y motivo.
- No inventes estados de sensores.

Memoria relevante:
{memory_block}

Observaciones recientes:
{observation_block}

Tareas pendientes:
{task_block}
""".strip()

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool = True,
        task: str = "chat",
        priority: str = "cost",
        max_budget_usd: float | None = None,
        preferred_model: str | None = None,
        requires_memory: bool = False,
    ) -> Any:
        selection = self.router.select(
            ModelRequest(
                task=task,
                prompt_tokens_estimate=estimate_prompt_tokens(messages),
                requires_tools=tools and bool(self.openai_tools),
                requires_memory=requires_memory,
                priority=priority,
                max_budget_usd=max_budget_usd,
                preferred_model=preferred_model,
            )
        )
        kwargs: dict[str, Any] = {
            "model": selection.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools and self.openai_tools:
            kwargs["tools"] = self.openai_tools
            kwargs["tool_choice"] = "auto"

        last_exc: Exception | None = None
        attempted = [selection.model, *selection.fallbacks]
        for index, model in enumerate(attempted):
            kwargs["model"] = model
            started = time.perf_counter()
            try:
                response = await self.client.chat.completions.create(**kwargs)
                duration_ms = int((time.perf_counter() - started) * 1000)
                reason = selection.reason if index == 0 else f"fallback tras fallo: {last_exc}"
                self.record_usage(
                    response,
                    model=model,
                    task=task,
                    reason=reason,
                    estimated_pre_cost_usd=selection.estimated_cost_usd if index == 0 else None,
                    duration_ms=duration_ms,
                )
                return response
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.memory.add_event(
                    level="warn",
                    message=f"Fallo modelo {model} para tarea {task}: {exception_summary(exc)}",
                )
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ModelRouter no devolvió modelos candidatos")

    def record_usage(
        self,
        response: Any,
        *,
        model: str,
        task: str,
        reason: str,
        estimated_pre_cost_usd: float | None,
        duration_ms: int,
    ) -> None:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        estimated_cost = estimated_pre_cost_usd
        prices = self.router.price_tuple(model)
        if prices and prompt_tokens is not None and completion_tokens is not None:
            input_price, output_price = prices
            estimated_cost = (prompt_tokens * input_price) + (completion_tokens * output_price)
        provider = None
        extra = getattr(response, "model_extra", None)
        if isinstance(extra, dict):
            provider = extra.get("provider") or extra.get("provider_name")
        self.memory.add_usage_event(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            context=task,
            provider=provider,
            duration_ms=duration_ms,
            router_reason=reason,
        )
        runtime_log(
            "info",
            "llm",
            "llm_call",
            task=task,
            model=model,
            provider=provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            duration_ms=duration_ms,
            reason=reason,
        )

    async def ask(self, user_text: str, task: str = "homeassistant") -> str:
        async with self.ask_lock:
            return await self._ask_unlocked(user_text, task=task)

    async def _ask_unlocked(self, user_text: str, *, task: str) -> str:
        working_messages = [
            {"role": "system", "content": self._system_prompt(user_text)},
            *self.messages[-MAX_HISTORY_MESSAGES:],
            {"role": "user", "content": user_text},
        ]

        answer = ""
        used_tools = False
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._chat(
                working_messages,
                tools=True,
                task=task,
                requires_memory=True,
            )
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                answer = msg.content or ""
                if not answer.strip() and used_tools:
                    answer = await self.summarize_tool_results(working_messages)
                working_messages.append({"role": "assistant", "content": answer})
                break

            working_messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else call
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                used_tools = True
                public_name = call.function.name
                real_name = self.tool_name_map.get(public_name, public_name)
                try:
                    tool_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}

                try:
                    if real_name.startswith("__builtin__:"):
                        print(f"\n[interno] {real_name.removeprefix('__builtin__:')} {compact_json(tool_args, 800)}")
                        content_text = await self.call_builtin_tool(real_name, tool_args)
                    else:
                        print(f"\n[MCP] {real_name} {compact_json(tool_args, 800)}")
                        result = await self.session.call_tool(real_name, tool_args)
                        content_text = tool_result_to_text(result) or "(sin contenido)"
                except Exception as exc:  # noqa: BLE001 - se devuelve al modelo como resultado de herramienta
                    content_text = f"ERROR ejecutando {real_name}: {exc}"
                    self.memory.add_event(level="error", message=content_text)

                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content_text,
                    }
                )
        else:
            answer = "He parado la cadena de herramientas porque superó el límite interno."

        self.messages.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": answer},
            ]
        )
        self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
        await self.learn_from_turn(user_text, answer)
        return answer

    async def summarize_tool_results(self, working_messages: list[dict[str, Any]]) -> str:
        response = await self._chat(
            [
                *working_messages,
                {
                    "role": "user",
                    "content": (
                        "Resume los resultados de las herramientas anteriores y contesta a la pregunta original. "
                        "Si hay datos numéricos, incluye entidad, valor, unidad y hora."
                    ),
                },
            ],
            tools=False,
            task="summary",
        )
        return response.choices[0].message.content or "He consultado las herramientas, pero no pude generar un resumen."

    async def execute_scheduled_instruction(self, task_id: int, title: str, instruction: str) -> str:
        simple_message = self.simple_reminder_message(instruction)
        if simple_message:
            print(f"\n[recordatorio #{task_id}] {simple_message}")
            return simple_message

        prompt = (
            "Ejecuta ahora esta tarea programada. "
            "No la reprogrames y no crees nuevas tareas salvo que sea imprescindible para terminarla. "
            "Responde muy breve indicando qué hiciste.\n"
            f"Tarea #{task_id}: {title}\n"
            f"Instrucción: {instruction}"
        )
        async with self.ask_lock:
            return await self._run_task_unlocked(prompt)

    def simple_reminder_message(self, instruction: str) -> str | None:
        text = instruction.strip()
        lowered = text.lower()
        prefixes = (
            "decir ",
            "dime ",
            "avisame ",
            "avísame ",
            "recordarme ",
            "recuerdame ",
            "recuérdame ",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip() or text
        match = re.search(r"\bdecir\s+(.+)$", text, flags=re.IGNORECASE)
        if match and not any(word in lowered for word in ("enciende", "apaga", "activa", "desactiva", "abre", "cierra", "pon ")):
            return match.group(1).strip()
        if not any(word in lowered for word in ("enciende", "apaga", "activa", "desactiva", "abre", "cierra", "pon ")):
            return text
        return None

    async def _run_task_unlocked(self, user_text: str) -> str:
        working_messages = [
            {"role": "system", "content": self._system_prompt(user_text)},
            {"role": "user", "content": user_text},
        ]
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._chat(working_messages, tools=True, task="scheduled_task")
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                return msg.content or "Tarea ejecutada sin respuesta del modelo."

            working_messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else call
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                public_name = call.function.name
                real_name = self.tool_name_map.get(public_name, public_name)
                try:
                    tool_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}

                if real_name.startswith("__builtin__:"):
                    content_text = await self.call_builtin_tool(real_name, tool_args)
                else:
                    print(f"\n[MCP tarea] {real_name} {compact_json(tool_args, 800)}")
                    result = await self.session.call_tool(real_name, tool_args)
                    content_text = tool_result_to_text(result) or "(sin contenido)"
                working_messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content_text}
                )
        return "Tarea detenida: superó el límite interno de herramientas."

    async def call_builtin_tool(self, real_name: str, args: dict[str, Any]) -> str:
        if real_name == "__builtin__:schedule_task":
            return await self.schedule_task(args)
        if real_name == "__builtin__:list_scheduled_tasks":
            return await self.list_scheduled_tasks(args)
        if real_name == "__builtin__:edit_scheduled_task":
            return await self.edit_scheduled_task(args)
        if real_name == "__builtin__:cancel_scheduled_task":
            return await self.cancel_scheduled_task(args)
        indexed_name = real_name.removeprefix("__builtin__:")
        if indexed_name in builtin_tool_names(include_homeassistant=self.has_homeassistant_rest):
            return await call_indexed_builtin_tool(self, indexed_name, args)
        raise ValueError(f"Herramienta interna desconocida: {real_name}")

    async def schedule_task(self, args: dict[str, Any]) -> str:
        run_at = str(args.get("run_at", "")).strip()
        title = str(args.get("title", "")).strip()
        instruction = str(args.get("instruction", "")).strip()
        if not run_at or not title or not instruction:
            raise ValueError("run_at, title e instruction son obligatorios")
        task_id = self.memory.add_task(run_at=run_at, title=title, instruction=instruction)
        task = self.memory.list_tasks(include_done=True, limit=200)
        created = next((row for row in task if row["id"] == task_id), None)
        return compact_json(
            {
                "created": True,
                "task_id": task_id,
                "run_at_utc": created["run_at"] if created else parse_datetime_to_utc(run_at),
                "title": title,
                "instruction": instruction,
            }
        )

    async def list_scheduled_tasks(self, args: dict[str, Any]) -> str:
        include_done = bool(args.get("include_done", False))
        limit = int(args.get("limit", 30) or 30)
        rows = self.memory.list_tasks(include_done=include_done, limit=max(1, min(limit, 100)))
        return compact_json([dict(row) for row in rows], max_chars=12000)

    async def edit_scheduled_task(self, args: dict[str, Any]) -> str:
        task_id = int(args.get("task_id"))
        updated = self.memory.update_task(
            task_id=task_id,
            run_at=str(args["run_at"]).strip() if args.get("run_at") else None,
            title=str(args["title"]).strip() if args.get("title") else None,
            instruction=str(args["instruction"]).strip() if args.get("instruction") else None,
            status=str(args["status"]).strip() if args.get("status") else None,
        )
        return compact_json({"updated": updated, "task_id": task_id})

    async def cancel_scheduled_task(self, args: dict[str, Any]) -> str:
        task_id = int(args.get("task_id"))
        cancelled = self.memory.cancel_task(task_id)
        return compact_json({"cancelled": cancelled, "task_id": task_id})

    async def learn_from_turn(self, user_text: str, answer: str) -> None:
        prompt = f"""
Extrae memorias duraderas útiles de esta interacción.
Devuelve JSON estricto con esta forma:
{{"memories":[{{"kind":"preferencia|hecho|patron|instruccion","topic":"...","content":"...","confidence":0.0}}]}}

Guarda solo información que pueda ser útil en futuras conversaciones o para interpretar sensores.
No guardes saludos, contenido temporal trivial ni estados instantáneos salvo que indiquen un patrón.

Usuario: {user_text}
Asistente: {answer}
""".strip()
        try:
            response = await self._chat(
                [
                    {"role": "system", "content": "Eres un extractor de memoria. Responde solo JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                tools=False,
                task="memory_extraction",
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            self.memory.add_event(level="warn", message=f"No se pudo extraer memoria: {exc}")
            return

        for item in data.get("memories", []):
            content = str(item.get("content", "")).strip()
            topic = str(item.get("topic", "")).strip() or "general"
            kind = str(item.get("kind", "hecho")).strip() or "hecho"
            if len(content) < 12:
                continue
            confidence = float(item.get("confidence", 0.7))
            self.memory.add_memory(
                kind=kind,
                topic=topic,
                content=content,
                confidence=max(0.0, min(1.0, confidence)),
                source="conversation",
            )

    async def observe_sensors(self, sensor_prompt: str) -> None:
        prompt = f"""
Haz una observación de sensores de Home Assistant usando MCP.

Instrucciones del usuario/configuración:
{sensor_prompt}

Devuelve una respuesta breve con:
- estados relevantes
- cambios o anomalías
- riesgo bajo/medio/alto
- una recomendación si procede
""".strip()
        answer = await self.ask(prompt)
        self.memory.add_observation(source="sensor-loop", summary=answer, raw=None)


async def sensor_loop(agent: SegurAIAgent, poll_seconds: int, sensor_prompt: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await agent.observe_sensors(sensor_prompt)
        except Exception as exc:  # noqa: BLE001
            agent.memory.add_event(level="error", message=f"Fallo en observación de sensores: {exc}")
            print(f"\n[observador] error: {exc}")

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


async def task_loop(agent: SegurAIAgent, stop: asyncio.Event) -> None:
    agent.memory.reset_running_tasks()
    runtime_log("info", "task_loop", "started")
    while not stop.is_set():
        due_tasks = agent.memory.get_due_tasks(limit=5)
        for task in due_tasks:
            task_id = int(task["id"])
            if not agent.memory.mark_task_running(task_id):
                continue
            try:
                print(f"\n[tarea #{task_id}] ejecutando: {task['title']}")
                runtime_log("info", "task_loop", "task_started", task_id=task_id, title=str(task["title"]))
                result = await asyncio.wait_for(
                    agent.execute_scheduled_instruction(
                        task_id=task_id,
                        title=str(task["title"]),
                        instruction=str(task["instruction"]),
                    ),
                    timeout=TASK_TIMEOUT_SECONDS,
                )
                agent.memory.mark_task_done(task_id, result)
                runtime_log("info", "task_loop", "task_done", task_id=task_id, result=result[:500])
                print(f"\n[tarea #{task_id}] completada")
            except asyncio.TimeoutError:
                message = f"Tiempo agotado tras {TASK_TIMEOUT_SECONDS}s"
                agent.memory.mark_task_failed(task_id, message)
                runtime_log("warn", "task_loop", "task_timeout", task_id=task_id, timeout=TASK_TIMEOUT_SECONDS)
                print(f"\n[tarea #{task_id}] {message}")
            except Exception as exc:  # noqa: BLE001
                agent.memory.mark_task_failed(task_id, exception_summary(exc))
                runtime_log("error", "task_loop", "task_failed", task_id=task_id, error=exception_summary(exc))
                print(f"\n[tarea #{task_id}] error: {exc}")

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=10)


def history_file_path() -> Path:
    return Path(DEFAULT_HISTORY_FILE).expanduser()


def setup_console_history() -> None:
    if readline is None:
        runtime_log("warn", "console", "readline no disponible; historial de flechas desactivado")
        return
    history_path = history_file_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        readline.read_history_file(str(history_path))
    readline.set_history_length(DEFAULT_HISTORY_LIMIT)
    for binding in (
        "tab: complete",
        '"\\e[A": history-search-backward',
        '"\\e[B": history-search-forward',
    ):
        with contextlib.suppress(Exception):
            readline.parse_and_bind(binding)
    atexit.register(save_console_history)


def save_console_history() -> None:
    if readline is None:
        return
    history_path = history_file_path()
    with contextlib.suppress(Exception):
        history_path.parent.mkdir(parents=True, exist_ok=True)
        readline.set_history_length(DEFAULT_HISTORY_LIMIT)
        readline.write_history_file(str(history_path))
        history_path.chmod(0o600)


def remember_console_line(line: str) -> None:
    if readline is None:
        return
    text = line.strip()
    if not text:
        return
    current_len = readline.get_current_history_length()
    previous = readline.get_history_item(current_len) if current_len else None
    if previous != text:
        readline.add_history(text)
    save_console_history()


async def async_input(prompt: str) -> str:
    line = await asyncio.to_thread(input, prompt)
    remember_console_line(line)
    return line


def print_rows(title: str, rows: list[sqlite3.Row], empty: str) -> None:
    print(f"\n{title}")
    if not rows:
        print(empty)
        return
    for row in rows:
        if "content" in row.keys():
            print(f"- #{row['id']} {row['topic']}: {row['content']}")
        else:
            print(f"- #{row['id']} {row['created_at']} {row['source']}: {row['summary']}")


def print_tasks(rows: list[sqlite3.Row]) -> None:
    print("\nTareas")
    if not rows:
        print("Sin tareas pendientes.")
        return
    for row in rows:
        detail = row["last_error"] or row["result"] or row["instruction"]
        print(f"- #{row['id']} {row['run_at']} [{row['status']}] {row['title']}: {detail}")


def print_usage_summary(summary: dict[str, Any]) -> None:
    total = summary["total"]
    print("\nCoste LLM")
    print(
        "Total: "
        f"{total['calls']} llamadas, "
        f"{total['prompt_tokens']} entrada, "
        f"{total['completion_tokens']} salida, "
        f"{total['total_tokens']} tokens, "
        f"${total['cost']:.6f} estimado"
    )
    print("Recientes:")
    for row in summary["recent"]:
        cost = row["estimated_cost_usd"]
        cost_text = f"${cost:.6f}" if cost is not None else "sin estimar"
        duration = f"{row['duration_ms']}ms" if row.get("duration_ms") is not None else "sin duracion"
        provider = row.get("provider") or "proveedor no informado"
        print(
            f"- {row['created_at']} {row['context']} {row['model']}: "
            f"{row['prompt_tokens']}/{row['completion_tokens']} tokens, {cost_text}, {duration}, {provider}"
        )
        if row.get("router_reason"):
            print(f"  motivo: {row['router_reason']}")


def mask_secret(value: str | None, *, visible: int = 4) -> str:
    if not value:
        return "no configurado"
    if len(value) <= visible * 2:
        return "configurado"
    return f"{value[:visible]}...{value[-visible:]}"


def write_env_values(updates: dict[str, str], env_path: Path = Path(".env")) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")


def print_help() -> None:
    print(
        textwrap.dedent(
            """
            Comandos:
              /help, /ayuda, /comandos
                                      Muestra esta ayuda completa
              /configuracion          Ver configuracion activa y como cambiar Home Assistant
              /configuracion hogar <url> | <token>
                                      Guarda nuevo Home Assistant en .env para el proximo arranque
              /estado                 Ver estado resumido de SegurAI
              /salud                  Diagnostico rapido del sistema
              /herramientas           Ver herramientas MCP e internas disponibles
              /router                 Ver configuracion de ModelRouter
              /coste                  Ver tokens y coste estimado
              /logs [n]               Ver ultimas lineas del log runtime
              /memoria                Ver memorias recientes
              /sensores               Ver observaciones recientes
              /aprender <texto>       Guardar una memoria manual
              /tareas                 Ver tareas pendientes
              /tareas --todo          Ver tambien tareas completadas/canceladas
              /cancelar <id>          Cancelar tarea
              /reintentar <id>        Reactivar tarea cancelada o fallida
              /editar <id> | <ISO> | <titulo> | <instruccion>
                                      Editar una tarea programada
              /agentes                Listar agentes especializados
              /agente descubrir       Recargar descubrimiento de agentes
              /agente iniciar <nombre>
              /agente detener <nombre>
              /agente reiniciar <nombre>
              /agente run <nombre>
              /ls <ruta>              Listar directorio permitido
              /leer <ruta>            Leer fichero de texto permitido
              /escribir <ruta> | <texto>
                                      Crear fichero de texto permitido
              /borrar <ruta>          Borrar fichero/directorio vacio
              /salir                  Cerrar

            Tambien puedes pedir en lenguaje natural: leer sensores, consultar historico,
            encender/apagar dispositivos, programar tareas, escribir ficheros o buscar en web.
            """
        ).strip()
    )


def print_configuration(agent: SegurAIAgent, config: RuntimeConfig, agent_manager: AgentManager) -> None:
    usage = agent.memory.usage_summary(limit=0)["total"]
    print(
        textwrap.dedent(
            f"""
            Configuracion SegurAI
            Home Assistant MCP URL: {config.mcp_url or "no configurado"}
            Home Assistant base REST: {config.ha_base_url or "no configurado"}
            HA_TOKEN: {mask_secret(config.ha_token)}
            REST Home Assistant: {"activo" if agent.has_homeassistant_rest else "inactivo"}
            Acciones requieren confirmacion: {"si" if config.require_action_confirmation else "no"}

            Modelo default: {agent.router.describe()["default"]}
            Rutas modelos: {config.model_routes_path}
            DB memoria: {config.db_path}
            Agents dir: {config.agents_dir}
            Agentes cargados: {len(agent_manager.agents)}
            Agentes activos: {len(agent_manager.active_agents())}

            Observador sensores: {"activo" if config.enable_sensor_loop else "desactivado"}
            Intervalo sensores: {config.poll_seconds}s
            Timeout tareas: {TASK_TIMEOUT_SECONDS}s
            Tareas pendientes: {len(agent.memory.list_tasks(include_done=False, limit=100))}

            FS permitido: {", ".join(str(root) for root in config.fs_roots)}
            Log runtime: {Path(DEFAULT_LOG_FILE).expanduser()}
            Historial consola: {history_file_path()}
            Herramientas disponibles: {len(agent.tool_names)}
            Coste estimado acumulado: ${usage['cost']:.6f}
            .env: {Path('.env').resolve()}

            Cambiar Home Assistant para el proximo arranque:
            /configuracion hogar https://nuevo-home-assistant/api/mcp | TOKEN
            """
        ).strip()
    )


def handle_configuration_command(rest: str, agent: SegurAIAgent, config: RuntimeConfig, agent_manager: AgentManager) -> None:
    text = rest.strip()
    if not text or text in {"ver", "mostrar", "show"}:
        print_configuration(agent, config, agent_manager)
        return
    action, _, payload = text.partition(" ")
    if action not in {"hogar", "home", "ha", "homeassistant"}:
        print("Uso: /configuracion hogar <HA_MCP_URL> | <HA_TOKEN>")
        return
    parts = [part.strip() for part in payload.split("|", 1)]
    if not parts or not parts[0]:
        print("Uso: /configuracion hogar <HA_MCP_URL> | <HA_TOKEN>")
        return
    updates = {"HA_MCP_URL": parts[0]}
    if len(parts) > 1 and parts[1]:
        updates["HA_TOKEN"] = parts[1]
    write_env_values(updates)
    print("Configuracion guardada en .env.")
    print("Reinicia SegurAI para conectar al nuevo Home Assistant.")
    print(f"Nuevo HA_MCP_URL: {updates['HA_MCP_URL']}")
    print(f"Nuevo HA_TOKEN: {mask_secret(updates.get('HA_TOKEN') or config.ha_token)}")


def print_health(agent: SegurAIAgent, agent_manager: AgentManager, config: RuntimeConfig) -> None:
    usage = agent.memory.usage_summary(limit=0)["total"]
    events = agent.memory.recent_events(limit=5)
    failed_tasks = [
        row for row in agent.memory.list_tasks(include_done=True, limit=100) if row["status"] == "failed"
    ]
    load_errors = len(agent_manager.load_errors)
    agent_rows = agent_manager.list_agents()
    failures = sum(row["stats"]["failures"] for row in agent_rows)
    status = "OK"
    if load_errors or failures or failed_tasks:
        status = "WARNING"
    print(
        textwrap.dedent(
            f"""
            Salud SegurAI: {status}
            MCP: conectado
            ModelRouter: {config.model_routes_path}
            Agentes: {len(agent_rows)} cargados, {len(agent_manager.active_agents())} activos, {failures} fallos
            Errores de carga agentes: {load_errors}
            Tareas fallidas: {len(failed_tasks)}
            Coste estimado acumulado: ${usage['cost']:.6f}
            Log: {Path(DEFAULT_LOG_FILE).expanduser()}
            """
        ).strip()
    )
    if events:
        print("\nÚltimos eventos:")
        for event in events:
            print(f"- {event['created_at']} [{event['level']}] {event['message']}")


async def terminal_loop(
    agent: SegurAIAgent,
    config: RuntimeConfig,
    stop: asyncio.Event,
    agent_manager: AgentManager,
) -> None:
    print(f"\n{APP_NAME} listo. Escribe /help para comandos.")
    while not stop.is_set():
        try:
            user_text = (await async_input("\nSegurAI> ")).strip()
        except (EOFError, KeyboardInterrupt):
            stop.set()
            break

        if not user_text:
            continue

        command, _, rest = user_text.partition(" ")
        if command in {"/salir", "/exit", "/quit"}:
            stop.set()
            break
        if command in {"/help", "/ayuda", "/comandos"}:
            print_help()
            continue
        if command in {"/configuracion", "/configuración", "/config"}:
            handle_configuration_command(rest, agent, config, agent_manager)
            continue
        if command == "/memoria":
            print_rows("Memoria", agent.memory.recent_memories(20), "Sin memorias guardadas.")
            continue
        if command == "/sensores":
            print_rows("Observaciones", agent.memory.recent_observations(20), "Sin observaciones guardadas.")
            continue
        if command == "/tareas":
            print_tasks(agent.memory.list_tasks(include_done="--todo" in rest, limit=50))
            continue
        if command == "/agentes":
            print(json.dumps(agent_manager.list_agents(), ensure_ascii=False, indent=2))
            if agent_manager.load_errors:
                print("\nErrores de carga:")
                print(json.dumps(agent_manager.load_errors, ensure_ascii=False, indent=2))
            continue
        if command == "/agente":
            parts = rest.split()
            action = parts[0] if parts else ""
            name = parts[1] if len(parts) > 1 else ""
            if action == "descubrir":
                agent_manager.discover()
                print("Agentes redescubiertos.")
                continue
            if not name or action not in {"iniciar", "detener", "reiniciar", "run"}:
                print("Uso: /agente iniciar|detener|reiniciar|run <nombre> o /agente descubrir")
                continue
            if action == "iniciar":
                print("Agente iniciado." if await agent_manager.start(name) else "No encontré ese agente.")
                continue
            if action == "detener":
                print("Agente detenido." if await agent_manager.stop(name) else "No encontré ese agente.")
                continue
            if action == "reiniciar":
                print("Agente reiniciado." if await agent_manager.restart(name) else "No encontré ese agente.")
                continue
            if action == "run":
                result = await agent_manager.run_once(name)
                print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
                continue
        if command == "/cancelar":
            try:
                task_id = int(rest.strip())
            except ValueError:
                print("Uso: /cancelar <id>")
                continue
            print("Tarea cancelada." if agent.memory.cancel_task(task_id) else "No encontré esa tarea.")
            continue
        if command == "/reintentar":
            try:
                task_id = int(rest.strip())
            except ValueError:
                print("Uso: /reintentar <id>")
                continue
            updated = agent.memory.update_task(task_id=task_id, status="pending")
            print("Tarea reactivada." if updated else "No encontré esa tarea.")
            continue
        if command == "/editar":
            parts = [part.strip() for part in rest.split("|")]
            if len(parts) != 4:
                print("Uso: /editar <id> | <ISO> | <titulo> | <instruccion>")
                continue
            try:
                task_id = int(parts[0])
            except ValueError:
                print("El id debe ser numérico.")
                continue
            updated = agent.memory.update_task(
                task_id=task_id,
                run_at=parts[1],
                title=parts[2],
                instruction=parts[3],
                status="pending",
            )
            print("Tarea editada." if updated else "No encontré esa tarea.")
            continue
        if command == "/herramientas":
            print("\nHerramientas MCP:")
            for name in agent.tool_names:
                print(f"- {name}")
            continue
        if command == "/coste":
            print_usage_summary(agent.memory.usage_summary(limit=12))
            continue
        if command == "/logs":
            try:
                limit = int(rest.strip() or "50")
            except ValueError:
                limit = 50
            lines = tail_file(Path(DEFAULT_LOG_FILE).expanduser(), limit=limit)
            if not lines:
                print("Sin logs todavía.")
            else:
                print("\n".join(lines))
            continue
        if command == "/salud":
            print_health(agent, agent_manager, config)
            continue
        if command == "/router":
            print(json.dumps(agent.router.describe(), ensure_ascii=False, indent=2))
            continue
        if command == "/ls":
            path = rest.strip() or "."
            try:
                print(await call_indexed_builtin_tool(agent, "fs_list_dir", {"path": path, "limit": 100}))
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/leer":
            path = rest.strip()
            if not path:
                print("Uso: /leer <ruta>")
                continue
            try:
                result = json.loads(await call_indexed_builtin_tool(agent, "fs_read_file", {"path": path, "max_chars": 20000}))
                print(result["content"])
                if result.get("truncated"):
                    print("\n[truncado]")
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/escribir":
            parts = [part.strip() for part in rest.split("|", 1)]
            if len(parts) != 2:
                print("Uso: /escribir <ruta> | <texto>")
                continue
            try:
                print(await call_indexed_builtin_tool(agent, "fs_write_file", {"path": parts[0], "content": parts[1], "overwrite": False}))
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/borrar":
            path = rest.strip()
            if not path:
                print("Uso: /borrar <ruta>")
                continue
            try:
                print(await call_indexed_builtin_tool(agent, "fs_delete_path", {"path": path, "confirm": True, "recursive": False}))
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/estado":
            print(
                textwrap.dedent(
                    f"""
                    Router modelos: {config.model_routes_path}
                    Modelo default: {agent.router.describe()["default"]}
                    DB: {config.db_path}
                    Agents dir: {config.agents_dir}
                    Agentes activos: {len(agent_manager.active_agents())}
                    Observador: {"activo" if config.enable_sensor_loop else "desactivado"}
                    Intervalo sensores: {config.poll_seconds}s
                    Herramientas MCP: {len(agent.tool_names)}
                    Tareas pendientes: {len(agent.memory.list_tasks(include_done=False, limit=100))}
                    Timeout tareas: {TASK_TIMEOUT_SECONDS}s
                    FS permitido: {", ".join(str(root) for root in config.fs_roots)}
                    Coste estimado: ${agent.memory.usage_summary(limit=0)["total"]["cost"]:.6f}
                    """
                ).strip()
            )
            continue
        if command == "/aprender":
            content = rest.strip()
            if not content:
                print("Uso: /aprender <texto>")
                continue
            agent.memory.add_memory(kind="hecho", topic="manual", content=content, confidence=1.0, source="manual")
            print("Memoria guardada.")
            continue

        try:
            answer = await agent.ask(user_text)
        except Exception as exc:  # noqa: BLE001
            agent.memory.add_event(level="error", message=f"Fallo respondiendo: {exc}")
            print(f"\nError: {exc}")
            continue
        print(f"\n{answer}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agente terminal 24/7 con MCP Home Assistant, OpenRouter/DeepSeek y memoria local."
    )
    parser.add_argument("--db", default=os.getenv("SEGURAI_DB", DEFAULT_DB), help="Ruta SQLite de memoria.")
    parser.add_argument(
        "--model-routes",
        default=os.getenv("SEGURAI_MODEL_ROUTES", DEFAULT_MODEL_ROUTES),
        help="Archivo YAML/JSON con reglas de seleccion de modelos.",
    )
    parser.add_argument(
        "--agents-dir",
        default=os.getenv("SEGURAI_AGENTS_DIR", DEFAULT_AGENTS_DIR),
        help="Directorio con modulos de agentes especializados.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("SEGURAI_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
        help="Intervalo de observación de sensores.",
    )
    parser.add_argument(
        "--no-sensor-loop",
        action="store_true",
        help="Desactiva el observador 24/7 de sensores.",
    )
    parser.add_argument(
        "--allow-actions-without-confirmation",
        action="store_true",
        help="Permite acciones MCP sin confirmación explícita previa.",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Arranca como servicio 24/7 sin prompt interactivo; mantiene tareas y observadores.",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.getenv("HA_MCP_URL"),
        help="URL MCP HTTP de Home Assistant. También puede venir de HA_MCP_URL.",
    )
    parser.add_argument(
        "--ha-token",
        default=os.getenv("HA_TOKEN"),
        help="Token de larga duración de Home Assistant. También puede venir de HA_TOKEN.",
    )
    parser.add_argument(
        "--fs-roots",
        default=DEFAULT_FS_ROOTS,
        help="Rutas permitidas para leer/escribir/borrar, separadas por coma. Por defecto: directorio actual.",
    )
    parser.add_argument(
        "mcp_cmd",
        nargs=argparse.REMAINDER,
        help="Comando del servidor MCP después de --",
    )
    return parser.parse_args()


async def run_agent_session(
    *,
    session: ClientSession,
    client: AsyncOpenAI,
    memory: MemoryStore,
    config: RuntimeConfig,
    stop: asyncio.Event,
    router: ModelRouter,
    live_context: LiveContextManager,
) -> None:
    await session.initialize()
    listed = await session.list_tools()
    tools = listed.tools
    agent = SegurAIAgent(
        client=client,
        router=router,
        session=session,
        memory=memory,
        tools=tools,
        require_action_confirmation=config.require_action_confirmation,
        ha_base_url=config.ha_base_url,
        ha_token=config.ha_token,
        fs_roots=config.fs_roots,
    )
    agent_manager = AgentManager(
        config.agents_dir,
        context_services={
            "segurai": agent,
            "memory": memory,
            "router": router,
            "mcp_session": session,
            "live_context": live_context,
        },
        event_logger=runtime_log,
    )
    agent_manager.discover()

    print(f"Conectado a MCP con {len(tools)} herramientas.")
    print(f"Agentes descubiertos: {len(agent_manager.agents)}")
    runtime_log("info", "segurai", "started", mcp_tools=len(tools), agents=len(agent_manager.agents))
    tasks = [asyncio.create_task(task_loop(agent, stop))]
    if not config.service_mode:
        tasks.insert(0, asyncio.create_task(terminal_loop(agent, config, stop, agent_manager)))
    else:
        print("SegurAI servicio 24/7 activo: prompt interactivo desactivado.")
    if config.enable_sensor_loop:
        tasks.append(
            asyncio.create_task(
                sensor_loop(agent, config.poll_seconds, config.sensor_prompt, stop)
            )
        )

    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> int:
    if load_dotenv is not None:
        load_dotenv()
    configure_runtime_defaults_from_env()
    setup_console_history()

    args = parse_args()
    if MISSING_DEPENDENCY:
        print(
            f"Falta la dependencia Python '{MISSING_DEPENDENCY}'. "
            "Instala primero con: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("Falta OPENROUTER_API_KEY. Ejemplo: export OPENROUTER_API_KEY='sk-or-...'", file=sys.stderr)
        return 2
    if args.mcp_cmd and args.mcp_cmd[0] == "--":
        args.mcp_cmd = args.mcp_cmd[1:]
    if not args.mcp_cmd and not args.mcp_url:
        print(
            "Falta conexión MCP. Usa HA_MCP_URL en .env, --mcp-url, o un comando después de --.",
            file=sys.stderr,
        )
        return 2

    config = RuntimeConfig(
        db_path=Path(args.db).expanduser(),
        model_routes_path=Path(args.model_routes).expanduser(),
        agents_dir=Path(args.agents_dir).expanduser(),
        poll_seconds=max(30, args.poll_seconds),
        sensor_prompt=os.getenv(
            "SEGURAI_SENSOR_PROMPT",
            "Consulta sensores de presencia, puertas, ventanas, movimiento, alarma y cámaras si existen.",
        ),
        enable_sensor_loop=not args.no_sensor_loop,
        require_action_confirmation=not args.allow_actions_without_confirmation,
        service_mode=args.service,
        mcp_url=args.mcp_url,
        mcp_cmd=args.mcp_cmd,
        ha_base_url=derive_ha_base_url(args.mcp_url),
        ha_token=args.ha_token,
        fs_roots=parse_fs_roots(args.fs_roots),
    )

    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    model_catalog = await fetch_openrouter_model_catalog()
    router = ModelRouter(config.model_routes_path, model_catalog)
    memory = MemoryStore(config.db_path)
    live_context = LiveContextManager()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        if config.mcp_url:
            if not args.ha_token:
                print("Falta HA_TOKEN para conectar al MCP HTTP de Home Assistant.", file=sys.stderr)
                return 2
            headers = {"Authorization": f"Bearer {args.ha_token}"}
            async with streamablehttp_client(config.mcp_url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await run_agent_session(
                        session=session,
                        client=client,
                        memory=memory,
                        config=config,
                        stop=stop,
                        router=router,
                        live_context=live_context,
                    )
        else:
            server_params = StdioServerParameters(
                command=config.mcp_cmd[0],
                args=config.mcp_cmd[1:],
                env=os.environ.copy(),
            )
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await run_agent_session(
                        session=session,
                        client=client,
                        memory=memory,
                        config=config,
                        stop=stop,
                        router=router,
                        live_context=live_context,
                    )
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo conectar o mantener la sesión MCP: {exception_summary(exc)}", file=sys.stderr)
        if config.mcp_url and "supervisor" in config.mcp_url:
            print(
                "La URL con host 'supervisor' normalmente solo resuelve dentro de Home Assistant. "
                "Desde esta máquina usa la URL externa de HA, por ejemplo http://IP_DE_HA:8123/api/mcp.",
                file=sys.stderr,
            )
        return 1
    finally:
        memory.close()

    print("\nSegurAI cerrado.")
    runtime_log("info", "segurai", "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
