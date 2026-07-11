from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tools.registry import builtin_tool_names

APP_NAME = "SegurAI"
DB_PATH = Path(os.getenv("SEGURAI_DB", "/config/data/segurai_memory.sqlite3"))
LOG_PATH = Path(os.getenv("SEGURAI_LOG_FILE", "/config/data/segurai_runtime.log"))

app = FastAPI(title="SegurAI", version="0.1.0")


def sqlite_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def usage_total() -> dict[str, Any]:
    rows = sqlite_rows(
        """
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) AS cost
        FROM usage_events
        """
    )
    return rows[0] if rows else {"calls": 0, "total_tokens": 0, "cost": 0.0}


def tail_log(limit: int = 80) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    tasks = sqlite_rows("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
    memories = sqlite_rows("SELECT COUNT(*) AS count FROM memories")
    observations = sqlite_rows("SELECT COUNT(*) AS count FROM observations")
    usage = usage_total()
    return {
        "app": APP_NAME,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "log_path": str(LOG_PATH),
        "log_exists": LOG_PATH.exists(),
        "tasks": tasks,
        "memories": memories[0]["count"] if memories else 0,
        "observations": observations[0]["count"] if observations else 0,
        "usage": usage,
        "tools": builtin_tool_names(include_homeassistant=bool(os.getenv("HA_TOKEN"))),
    }


@app.get("/api/memories")
def api_memories(limit: int = 20) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, created_at, kind, topic, content, confidence, source
        FROM memories
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )


@app.get("/api/tasks")
def api_tasks(limit: int = 30) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, run_at, status, title, instruction, result, last_error
        FROM tasks
        ORDER BY run_at ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )


@app.get("/api/logs")
def api_logs(limit: int = 80) -> dict[str, Any]:
    return {"lines": tail_log(max(1, min(limit, 500)))}


@app.get("/api/tools")
def api_tools() -> dict[str, Any]:
    return {"tools": builtin_tool_names(include_homeassistant=bool(os.getenv("HA_TOKEN")))}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SegurAI</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #17211f;
      --muted: #63716d;
      --line: #d7dfda;
      --panel: rgba(255,255,255,.86);
      --brand: #0f766e;
      --accent: #d97706;
      --wash: #eef7f1;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, rgba(15,118,110,.18), transparent 28rem),
        radial-gradient(circle at 90% 12%, rgba(217,119,6,.16), transparent 24rem),
        linear-gradient(135deg, #f7fbf8 0%, #edf5f2 52%, #f9f4e8 100%);
      min-height: 100vh;
    }
    main { max-width: 1180px; margin: 0 auto; padding: 32px 18px 48px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: end; margin-bottom: 22px; }
    h1 { font-size: clamp(2.4rem, 7vw, 5.6rem); line-height: .88; margin: 0; letter-spacing: 0; }
    h2 { font-size: 1rem; margin: 0 0 12px; font-family: ui-sans-serif, system-ui, sans-serif; }
    p { color: var(--muted); margin: 8px 0 0; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
    section, .stat {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: 0 18px 44px rgba(25, 45, 38, .08);
      backdrop-filter: blur(12px);
    }
    .stat { grid-column: span 3; min-height: 118px; }
    .stat strong { display: block; font: 700 2.1rem/1 ui-sans-serif, system-ui, sans-serif; }
    .wide { grid-column: span 8; }
    .side { grid-column: span 4; }
    .full { grid-column: 1 / -1; }
    ul { margin: 0; padding: 0; list-style: none; }
    li { padding: 9px 0; border-top: 1px solid var(--line); }
    li:first-child { border-top: 0; }
    code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    pre { white-space: pre-wrap; margin: 0; max-height: 320px; overflow: auto; color: #263631; }
    .pill { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; padding: 4px 9px; margin: 3px; background: rgba(255,255,255,.6); }
    .ok { color: var(--brand); }
    button { border: 0; background: var(--ink); color: white; border-radius: 7px; padding: 10px 14px; cursor: pointer; }
    @media (max-width: 820px) { .stat, .wide, .side { grid-column: 1 / -1; } header { display: block; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>SegurAI</h1>
        <p>Agente local para Home Assistant, memoria y decisiones vigiladas.</p>
      </div>
      <button onclick="loadAll()">Actualizar</button>
    </header>

    <div class="grid">
      <div class="stat"><p>Memorias</p><strong id="memories">-</strong></div>
      <div class="stat"><p>Observaciones</p><strong id="observations">-</strong></div>
      <div class="stat"><p>Llamadas LLM</p><strong id="calls">-</strong></div>
      <div class="stat"><p>Coste estimado</p><strong id="cost">-</strong></div>

      <section class="wide"><h2>Tareas</h2><ul id="tasks"></ul></section>
      <section class="side"><h2>Herramientas</h2><div id="tools"></div></section>
      <section class="wide"><h2>Memoria reciente</h2><ul id="memory"></ul></section>
      <section class="side"><h2>Estado</h2><pre id="status"></pre></section>
      <section class="full"><h2>Logs</h2><pre id="logs"></pre></section>
    </div>
  </main>
<script>
async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return r.json(); }
function text(v) { return v === null || v === undefined || v === '' ? '-' : String(v); }
async function loadAll() {
  const [status, memories, tasks, logs, tools] = await Promise.all([
    getJSON('/api/status'), getJSON('/api/memories'), getJSON('/api/tasks'), getJSON('/api/logs'), getJSON('/api/tools')
  ]);
  document.getElementById('memories').textContent = status.memories;
  document.getElementById('observations').textContent = status.observations;
  document.getElementById('calls').textContent = status.usage.calls;
  document.getElementById('cost').textContent = '$' + Number(status.usage.cost || 0).toFixed(5);
  document.getElementById('status').textContent = JSON.stringify(status, null, 2);
  document.getElementById('logs').textContent = logs.lines.join('\n') || 'Sin logs todavia.';
  document.getElementById('tools').innerHTML = tools.tools.map(t => `<span class="pill">${t}</span>`).join('');
  document.getElementById('tasks').innerHTML = tasks.length ? tasks.map(t => `<li><strong>#${t.id} ${t.status}</strong><br>${text(t.title)}<br><span class="mono">${text(t.run_at)}</span></li>`).join('') : '<li>Sin tareas.</li>';
  document.getElementById('memory').innerHTML = memories.length ? memories.map(m => `<li><strong>${text(m.topic)}</strong><br>${text(m.content)}</li>`).join('') : '<li>Sin memoria todavia.</li>';
}
loadAll().catch(err => { document.body.insertAdjacentHTML('beforeend', '<pre>' + err.message + '</pre>'); });
</script>
</body>
</html>
"""
