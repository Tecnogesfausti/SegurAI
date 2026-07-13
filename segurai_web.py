from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from agents.manager import AgentManager
from services.live_context.manager import LiveContextManager
from tools.registry import builtin_tool_names

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

APP_NAME = "SegurAI"


def default_data_dir() -> Path:
    if os.getenv("SUPERVISOR_TOKEN") or Path("/config").is_dir():
        return Path("/config/data")
    return Path(os.getenv("SEGURAI_DATA_DIR", "data"))


DATA_DIR = default_data_dir()
DB_PATH = Path(os.getenv("SEGURAI_DB", str(DATA_DIR / "segurai_memory.sqlite3")))
LOG_PATH = Path(os.getenv("SEGURAI_LOG_FILE", str(DATA_DIR / "segurai_runtime.log")))
AGENTS_DIR = Path(os.getenv("SEGURAI_AGENTS_DIR", "agents"))
AGENT_CONFIG_PATH = Path(os.getenv("SEGURAI_AGENT_CONFIG", str(DATA_DIR / "agent_config.json")))
CODEX_CONTEXT_PATH = Path(os.getenv("SEGURAI_CODEX_CONTEXT", str(DATA_DIR / "CODEX_CONTEXT.md")))
CODEX_NOTES_PATH = Path(os.getenv("SEGURAI_CODEX_NOTES", str(DATA_DIR / "CODEX_NOTES.md")))
BACKUP_DIR = Path(os.getenv("SEGURAI_BACKUP_DIR", str(DATA_DIR / "backups")))
BACKUP_KEY = os.getenv("SEGURAI_BACKUP_KEY", "")

app = FastAPI(title="SegurAI", version="0.2.0")


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def parse_run_at(value: str | None) -> str:
    if not value:
        return utc_now()
    text = value.strip()
    if not text:
        return utc_now()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run_at debe ser ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
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
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    additions = {
        "priority": "INTEGER NOT NULL DEFAULT 50",
        "interval_seconds": "INTEGER",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
    conn.commit()


def sqlite_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = connect_db()
    try:
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def execute_db(query: str, params: tuple[Any, ...] = ()) -> int:
    conn = connect_db()
    try:
        cursor = conn.execute(query, params)
        conn.commit()
        return int(cursor.lastrowid or cursor.rowcount)
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


def read_agent_config() -> dict[str, Any]:
    if not AGENT_CONFIG_PATH.exists():
        return {"agents": {}}
    try:
        data = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"agents": {}}
    if not isinstance(data, dict):
        return {"agents": {}}
    data.setdefault("agents", {})
    return data


def write_agent_config(data: dict[str, Any]) -> None:
    AGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class WebMemory:
    def add_observation(self, *, source: str, summary: str, raw: str | None = None) -> None:
        execute_db(
            "INSERT INTO observations(created_at, source, summary, raw) VALUES (?, ?, ?, ?)",
            (utc_now(), source[:80], summary.strip(), raw),
        )


@dataclasses.dataclass
class WebSegurAIContext:
    ha_base_url: str | None
    ha_token: str | None
    httpx: Any
    require_action_confirmation: bool = True

    @property
    def has_homeassistant_rest(self) -> bool:
        return bool(self.ha_base_url and self.ha_token and self.httpx is not None)


def build_agent_manager() -> AgentManager:
    manager = AgentManager(
        AGENTS_DIR,
        context_services={
            "memory": WebMemory(),
            "live_context": LiveContextManager(),
            "segurai": WebSegurAIContext(
                ha_base_url=os.getenv("HOME_ASSISTANT_URL") or os.getenv("HA_BASE_URL"),
                ha_token=os.getenv("HOME_ASSISTANT_TOKEN") or os.getenv("HA_TOKEN"),
                httpx=httpx,
            ),
        },
    )
    manager.discover()
    return manager


def agent_rows() -> list[dict[str, Any]]:
    manager = build_agent_manager()
    config = read_agent_config().get("agents", {})
    rows = []
    for row in manager.list_agents():
        overrides = config.get(row["name"], {}) if isinstance(config, dict) else {}
        row["enabled"] = bool(overrides.get("enabled", False))
        row["effective_priority"] = int(overrides.get("priority", row["priority"]))
        row["effective_frequency_seconds"] = int(overrides.get("frequency_seconds", row["frequency_seconds"]))
        row["overrides"] = overrides
        rows.append(row)
    return sorted(rows, key=lambda item: (-int(item["effective_priority"]), item["name"]))


def markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_Sin datos._\n"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ")[:180] for col in columns) + " |")
    return "\n".join([header, sep, *body]) + "\n"


def build_codex_context() -> str:
    agents = agent_rows()
    tasks = api_tasks(limit=12, include_done=True)
    observations = api_observations(limit=12)
    status = api_status()
    notes = CODEX_NOTES_PATH.read_text(encoding="utf-8", errors="replace") if CODEX_NOTES_PATH.exists() else ""
    log_tail = "\n".join(tail_log(40))
    return f"""# SegurAI Codex Maintenance Context

Generated: {utc_now()}

## Purpose

Use this file from an interactive Codex/tmux session to inspect, teach, correct and extend SegurAI.
SegurAI is the long-running service. Codex is the maintenance workshop.

## Important Paths

- App directory: `{Path.cwd()}`
- Agents directory: `{AGENTS_DIR}`
- Database: `{DB_PATH}`
- Runtime log: `{LOG_PATH}`
- Agent config: `{AGENT_CONFIG_PATH}`
- Codex context: `{CODEX_CONTEXT_PATH}`
- Codex notes: `{CODEX_NOTES_PATH}`
- Codex home: `{os.getenv("CODEX_HOME", "")}`
- Workspace: `{os.getenv("WORKSPACE") or os.getenv("SEGURAI_WORKSPACE", "")}`
- Codex model: `{os.getenv("CODEX_MODEL", "")}`

## Service Contract

- Prefer editing or adding agents in `{AGENTS_DIR}`.
- Do not execute physical Home Assistant actions without explicit confirmation.
- Prefer deterministic logic over LLM calls for thresholds, timestamps, distances and deduplication.
- After changes, run tests with `python -m unittest discover -s tests -t . -v` when available.
- Use the web UI/API to create tasks and run agents manually.

## Current Status

```json
{json.dumps(status, ensure_ascii=False, indent=2)}
```

## Agents

{markdown_table(agents, ['name', 'enabled', 'effective_priority', 'effective_frequency_seconds', 'description'])}

## Recent Tasks

{markdown_table(tasks, ['id', 'status', 'run_at', 'priority', 'title', 'result', 'last_error'])}

## Recent Observations

{markdown_table(observations, ['created_at', 'source', 'summary'])}

## Operator Notes

{notes or '_Sin notas todavía._'}

## Recent Log Tail

```text
{log_tail or 'Sin logs.'}
```
"""


def write_codex_context() -> str:
    CODEX_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = build_codex_context()
    CODEX_CONTEXT_PATH.write_text(text, encoding="utf-8")
    return text


def backup_sources() -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for path, arcname in [
        (DB_PATH, "data/segurai_memory.sqlite3"),
        (AGENT_CONFIG_PATH, "data/agent_config.json"),
        (CODEX_CONTEXT_PATH, "data/CODEX_CONTEXT.md"),
        (CODEX_NOTES_PATH, "data/CODEX_NOTES.md"),
        (LOG_PATH, "data/segurai_runtime.log"),
    ]:
        if path.exists():
            sources.append((path, arcname))
    if AGENTS_DIR.exists():
        sources.append((AGENTS_DIR, "agents"))
    return sources


def create_backup_archive(*, passphrase: str | None = None) -> dict[str, Any]:
    write_codex_context()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    tar_path = BACKUP_DIR / f"segurai-backup-{stamp}.tar.gz"
    manifest = {
        "created_at": utc_now(),
        "app": APP_NAME,
        "db_path": str(DB_PATH),
        "agents_dir": str(AGENTS_DIR),
        "included": [],
    }
    with tarfile.open(tar_path, "w:gz") as archive:
        for source, arcname in backup_sources():
            archive.add(source, arcname=arcname)
            manifest["included"].append(arcname)
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(dt.datetime.now(dt.UTC).timestamp())
        import io
        archive.addfile(info, io.BytesIO(manifest_bytes))
    key = passphrase if passphrase is not None else BACKUP_KEY
    if key:
        encrypted_path = tar_path.with_suffix(tar_path.suffix + ".enc")
        openssl = shutil.which("openssl")
        if not openssl:
            tar_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="openssl no esta disponible para cifrar el backup")
        subprocess.run(
            [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", str(tar_path), "-out", str(encrypted_path), "-pass", "stdin"],
            input=key.encode("utf-8"),
            check=True,
        )
        tar_path.unlink(missing_ok=True)
        return {"path": str(encrypted_path), "encrypted": True, "bytes": encrypted_path.stat().st_size, "manifest": manifest}
    return {"path": str(tar_path), "encrypted": False, "bytes": tar_path.stat().st_size, "manifest": manifest}


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    rows = []
    for path in sorted(BACKUP_DIR.glob("segurai-backup-*"), reverse=True):
        if path.is_file():
            rows.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size, "encrypted": path.name.endswith(".enc")})
    return rows


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
        "agent_config_path": str(AGENT_CONFIG_PATH),
        "tasks": tasks,
        "memories": memories[0]["count"] if memories else 0,
        "observations": observations[0]["count"] if observations else 0,
        "usage": usage,
        "tools": builtin_tool_names(include_homeassistant=bool(os.getenv("HA_TOKEN") or os.getenv("HOME_ASSISTANT_TOKEN"))),
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


@app.get("/api/observations")
def api_observations(limit: int = 30) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, created_at, source, summary, raw
        FROM observations
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )


@app.get("/api/tasks")
def api_tasks(limit: int = 50, include_done: bool = True) -> list[dict[str, Any]]:
    where = "" if include_done else "WHERE status IN ('pending', 'running', 'failed')"
    return sqlite_rows(
        f"""
        SELECT id, created_at, updated_at, run_at, status, title, instruction,
               result, last_error, priority, interval_seconds
        FROM tasks
        {where}
        ORDER BY run_at ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(limit, 200)),),
    )


@app.post("/api/tasks")
def api_create_task(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()
    if not title or not instruction:
        raise HTTPException(status_code=400, detail="title e instruction son obligatorios")
    run_at = parse_run_at(str(payload.get("run_at") or "") or None)
    priority = int(payload.get("priority", 50) or 50)
    interval = payload.get("interval_seconds")
    interval_seconds = int(interval) if interval not in (None, "") else None
    now = utc_now()
    task_id = execute_db(
        """
        INSERT INTO tasks(created_at, updated_at, run_at, status, title, instruction, priority, interval_seconds)
        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (now, now, run_at, title[:160], instruction, priority, interval_seconds),
    )
    return {"ok": True, "id": task_id}


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "title": "title",
        "instruction": "instruction",
        "status": "status",
        "priority": "priority",
        "interval_seconds": "interval_seconds",
    }
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [utc_now()]
    if "run_at" in payload:
        fields.append("run_at = ?")
        values.append(parse_run_at(str(payload.get("run_at") or "") or None))
    for key, column in allowed.items():
        if key in payload:
            value = payload[key]
            if key == "interval_seconds" and value in (None, ""):
                value = None
            elif key in {"priority", "interval_seconds"} and value is not None:
                value = int(value)
            elif value is not None:
                value = str(value)
            fields.append(f"{column} = ?")
            values.append(value)
    values.append(task_id)
    changed = execute_db(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", tuple(values))
    if changed < 1:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/{task_id}/run")
def api_run_task_now(task_id: int) -> dict[str, Any]:
    changed = execute_db(
        "UPDATE tasks SET updated_at = ?, run_at = ?, status = 'pending', last_error = NULL WHERE id = ?",
        (utc_now(), utc_now(), task_id),
    )
    if changed < 1:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: int) -> dict[str, Any]:
    changed = execute_db("UPDATE tasks SET updated_at = ?, status = 'cancelled' WHERE id = ?", (utc_now(), task_id))
    if changed < 1:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return {"ok": True, "id": task_id}


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: int) -> dict[str, Any]:
    changed = execute_db("DELETE FROM tasks WHERE id = ?", (task_id,))
    if changed < 1:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return {"ok": True, "id": task_id}


@app.get("/api/agents")
def api_agents() -> dict[str, Any]:
    manager = build_agent_manager()
    return {"agents": agent_rows(), "load_errors": manager.load_errors}


@app.patch("/api/agents/{name}")
def api_update_agent(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    known = {row["name"] for row in agent_rows()}
    if name not in known:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    data = read_agent_config()
    agents = data.setdefault("agents", {})
    current = agents.setdefault(name, {})
    for key in ("enabled", "priority", "frequency_seconds"):
        if key in payload:
            value = payload[key]
            if key == "enabled":
                current[key] = bool(value)
            else:
                current[key] = int(value)
    write_agent_config(data)
    return {"ok": True, "agent": name, "config": current}


@app.post("/api/agents/{name}/run")
async def api_run_agent(name: str) -> dict[str, Any]:
    manager = build_agent_manager()
    if name not in manager.agents:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    result = await manager.run_once(name)
    return {"ok": result.ok, "message": result.message, "data": result.data}






@app.get("/api/backups")
def api_backups() -> dict[str, Any]:
    return {"backup_dir": str(BACKUP_DIR), "backups": list_backups(), "key_configured": bool(BACKUP_KEY)}


@app.post("/api/backup")
def api_create_backup(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    passphrase = payload.get("passphrase")
    if passphrase is not None:
        passphrase = str(passphrase)
    return {"ok": True, "backup": create_backup_archive(passphrase=passphrase)}


@app.get("/api/codex/context")
def api_codex_context(refresh: bool = False) -> dict[str, Any]:
    text = write_codex_context() if refresh or not CODEX_CONTEXT_PATH.exists() else CODEX_CONTEXT_PATH.read_text(encoding="utf-8", errors="replace")
    return {"path": str(CODEX_CONTEXT_PATH), "text": text}


@app.post("/api/codex/context/refresh")
def api_refresh_codex_context() -> dict[str, Any]:
    text = write_codex_context()
    return {"ok": True, "path": str(CODEX_CONTEXT_PATH), "bytes": len(text.encode("utf-8"))}


@app.get("/api/codex/notes")
def api_codex_notes() -> dict[str, Any]:
    text = CODEX_NOTES_PATH.read_text(encoding="utf-8", errors="replace") if CODEX_NOTES_PATH.exists() else ""
    return {"path": str(CODEX_NOTES_PATH), "text": text}


@app.post("/api/codex/notes")
def api_update_codex_notes(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or "")
    CODEX_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_NOTES_PATH.write_text(text, encoding="utf-8")
    write_codex_context()
    return {"ok": True, "path": str(CODEX_NOTES_PATH)}


@app.get("/api/logs")
def api_logs(limit: int = 80) -> dict[str, Any]:
    return {"lines": tail_log(max(1, min(limit, 500)))}


@app.get("/api/tools")
def api_tools() -> dict[str, Any]:
    return {"tools": builtin_tool_names(include_homeassistant=bool(os.getenv("HA_TOKEN") or os.getenv("HOME_ASSISTANT_TOKEN")))}


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
      --panel: rgba(255,255,255,.9);
      --brand: #0f766e;
      --danger: #b91c1c;
      --warn: #b45309;
      --wash: #eef7f1;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: linear-gradient(135deg, #f7fbf8 0%, #edf5f2 52%, #f9f4e8 100%); min-height: 100vh; }
    main { max-width: 1280px; margin: 0 auto; padding: 24px 16px 44px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: end; margin-bottom: 18px; }
    h1 { font-size: clamp(2rem, 5vw, 4rem); line-height: .95; margin: 0; letter-spacing: 0; }
    h2 { font-size: 1rem; margin: 0 0 12px; }
    h3 { font-size: .95rem; margin: 14px 0 8px; }
    p { color: var(--muted); margin: 6px 0 0; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    section, .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; box-shadow: 0 12px 34px rgba(25, 45, 38, .08); }
    .stat { grid-column: span 3; min-height: 98px; }
    .stat strong { display: block; font-size: 1.8rem; line-height: 1; }
    .wide { grid-column: span 7; }
    .side { grid-column: span 5; }
    .full { grid-column: 1 / -1; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    label { display: grid; gap: 4px; color: var(--muted); font-size: .78rem; }
    input, textarea, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px; background: white; color: var(--ink); }
    textarea { min-height: 72px; resize: vertical; }
    button { border: 0; background: var(--ink); color: white; border-radius: 6px; padding: 8px 10px; cursor: pointer; }
    button.secondary { background: var(--brand); }
    button.warning { background: var(--warn); }
    button.danger { background: var(--danger); }
    button.ghost { color: var(--ink); background: white; border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; font-size: .88rem; }
    th, td { text-align: left; border-top: 1px solid var(--line); padding: 8px; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    pre { white-space: pre-wrap; margin: 0; max-height: 320px; overflow: auto; color: #263631; }
    .pill { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; margin: 2px; background: rgba(255,255,255,.7); }
    .row-actions { display: flex; flex-wrap: wrap; gap: 5px; }
    .muted { color: var(--muted); }
    .notice { position: sticky; top: 8px; z-index: 5; display: none; margin-bottom: 12px; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: white; }
    .notice.error { display: block; border-color: #fecaca; color: var(--danger); }
    .notice.ok { display: block; border-color: #99f6e4; color: var(--brand); }
    .split { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    @media (max-width: 900px) { .stat, .wide, .side { grid-column: 1 / -1; } header { display: block; } .split { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <div id="notice" class="notice"></div>
    <header>
      <div>
        <h1>SegurAI</h1>
        <p>Agentes, tareas, memoria y resultados desde la web.</p>
      </div>
      <div class="toolbar"><button onclick="loadAll()">Actualizar</button></div>
    </header>

    <div class="grid">
      <div class="stat"><p>Memorias</p><strong id="memories">-</strong></div>
      <div class="stat"><p>Observaciones</p><strong id="observations">-</strong></div>
      <div class="stat"><p>Llamadas LLM</p><strong id="calls">-</strong></div>
      <div class="stat"><p>Coste estimado</p><strong id="cost">-</strong></div>

      <section class="full">
        <h2>Agentes</h2>
        <table><thead><tr><th>Agente</th><th>Estado</th><th>Intervalo</th><th>Prioridad</th><th>Ultimo resultado</th><th></th></tr></thead><tbody id="agents"></tbody></table>
      </section>

      <section class="wide">
        <h2>Tareas</h2>
        <div class="split">
          <label>Titulo<input id="taskTitle" placeholder="Revision de sensores" /></label>
          <label>Fecha/hora<input id="taskRunAt" type="datetime-local" /></label>
          <label>Prioridad<input id="taskPriority" type="number" min="1" max="100" value="50" /></label>
          <label>Intervalo segundos<input id="taskInterval" type="number" min="0" placeholder="opcional" /></label>
        </div>
        <label>Instruccion<textarea id="taskInstruction" placeholder="Que debe hacer SegurAI"></textarea></label>
        <div class="toolbar"><button class="secondary" onclick="createTask()">Crear tarea</button></div>
        <table><thead><tr><th>ID</th><th>Estado</th><th>Plan</th><th>Resultado</th><th></th></tr></thead><tbody id="tasks"></tbody></table>
      </section>

      <section class="side"><h2>Herramientas</h2><div id="tools"></div></section>
      <section class="side">
        <h2>Backups</h2>
        <p class="muted" id="backupDir">-</p>
        <label>Clave backup opcional<input id="backupPassphrase" type="password" placeholder="usa SEGURAI_BACKUP_KEY si lo dejas vacio" /></label>
        <div class="toolbar"><button class="secondary" onclick="createBackup()">Crear backup</button></div>
        <ul id="backupList"></ul>
      </section>
      <section class="wide"><h2>Observaciones recientes</h2><ul id="observationList"></ul></section>
      <section class="side"><h2>Estado</h2><pre id="status"></pre></section>
      <section class="full">
        <h2>Codex mantenimiento</h2>
        <div class="toolbar"><button class="secondary" onclick="refreshCodexContext()">Actualizar contexto</button><span class="muted" id="codexPath">-</span></div>
        <label>Notas para enseñar/corregir SegurAI<textarea id="codexNotes" placeholder="Criterios, decisiones, errores conocidos, ideas de nuevos agentes..."></textarea></label>
        <div class="toolbar"><button onclick="saveCodexNotes()">Guardar notas</button></div>
        <pre id="codexContext"></pre>
      </section>
      <section class="full"><h2>Logs</h2><pre id="logs"></pre></section>
    </div>
  </main>
<script>
async function requestJSON(url, options = {}) {
  const r = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || url);
  return data;
}
function text(v) { return v === null || v === undefined || v === '' ? '-' : String(v); }
function html(v) { return text(v).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
function showNotice(message, kind = 'ok') {
  const node = document.getElementById('notice');
  node.textContent = message;
  node.className = 'notice ' + kind;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => { node.className = 'notice'; }, 6000);
}
function isoFromLocal(value) { return value ? new Date(value).toISOString() : null; }
async function loadAll() {
  const [status, observations, tasks, logs, tools, agents, codex, notes, backups] = await Promise.all([
    requestJSON('/api/status'), requestJSON('/api/observations'), requestJSON('/api/tasks'), requestJSON('/api/logs'), requestJSON('/api/tools'), requestJSON('/api/agents'), requestJSON('/api/codex/context'), requestJSON('/api/codex/notes'), requestJSON('/api/backups')
  ]);
  document.getElementById('memories').textContent = status.memories;
  document.getElementById('observations').textContent = status.observations;
  document.getElementById('calls').textContent = status.usage.calls;
  document.getElementById('cost').textContent = '$' + Number(status.usage.cost || 0).toFixed(5);
  document.getElementById('status').textContent = JSON.stringify(status, null, 2);
  document.getElementById('logs').textContent = logs.lines.join('\\n') || 'Sin logs todavia.';
  document.getElementById('tools').innerHTML = tools.tools.map(t => `<span class="pill">${html(t)}</span>`).join('');
  document.getElementById('codexPath').textContent = codex.path;
  document.getElementById('codexContext').textContent = codex.text;
  document.getElementById('codexNotes').value = notes.text || '';
  document.getElementById('backupDir').textContent = backups.backup_dir + (backups.key_configured ? ' · clave configurada' : ' · sin clave configurada');
  document.getElementById('backupList').innerHTML = backups.backups.length ? backups.backups.map(b => `<li><strong>${html(b.name)}</strong><br><span class="muted">${html(b.bytes)} bytes · ${html(b.path)}</span></li>`).join('') : '<li>Sin backups.</li>';
  renderTasks(tasks);
  renderAgents(agents.agents || []);
  document.getElementById('observationList').innerHTML = observations.length ? observations.map(o => `<li><strong>${html(o.source)}</strong> <span class="muted">${html(o.created_at)}</span><br>${html(o.summary)}</li>`).join('') : '<li>Sin observaciones todavia.</li>';
}
function renderAgents(rows) {
  document.getElementById('agents').innerHTML = rows.length ? rows.map(a => `
    <tr>
      <td><strong>${html(a.name)}</strong><br><span class="muted">${html(a.description)}</span></td>
      <td><label><input type="checkbox" ${a.enabled ? 'checked' : ''} onchange="updateAgent('${html(a.name)}', {enabled: this.checked})" /> activo</label></td>
      <td><input type="number" value="${a.effective_frequency_seconds}" min="1" onchange="updateAgent('${html(a.name)}', {frequency_seconds: this.value})" /></td>
      <td><input type="number" value="${a.effective_priority}" min="1" max="100" onchange="updateAgent('${html(a.name)}', {priority: this.value})" /></td>
      <td class="muted">${html(a.stats?.last_message || '-')}</td>
      <td><button class="secondary" onclick="runAgent('${html(a.name)}')">Ejecutar</button></td>
    </tr>`).join('') : '<tr><td colspan="6">Sin agentes.</td></tr>';
}
function renderTasks(rows) {
  document.getElementById('tasks').innerHTML = rows.length ? rows.map(t => `
    <tr>
      <td>#${t.id}<br><span class="muted">prio ${html(t.priority)}</span></td>
      <td>${html(t.status)}</td>
      <td><strong>${html(t.title)}</strong><br><span class="mono">${html(t.run_at)}</span><br>${html(t.instruction)}<br><span class="muted">intervalo: ${html(t.interval_seconds)}</span></td>
      <td>${html(t.result || t.last_error || '-')}</td>
      <td><div class="row-actions"><button class="secondary" onclick="runTask(${t.id})">Ejecutar</button><button class="warning" onclick="cancelTask(${t.id})">Cancelar</button><button class="danger" onclick="deleteTask(${t.id})">Eliminar</button></div></td>
    </tr>`).join('') : '<tr><td colspan="5">Sin tareas.</td></tr>';
}
async function createTask() {
  try {
    await requestJSON('/api/tasks', {method: 'POST', body: JSON.stringify({
      title: document.getElementById('taskTitle').value,
      instruction: document.getElementById('taskInstruction').value,
      run_at: isoFromLocal(document.getElementById('taskRunAt').value),
      priority: document.getElementById('taskPriority').value,
      interval_seconds: document.getElementById('taskInterval').value || null
    })});
    document.getElementById('taskTitle').value = '';
    document.getElementById('taskInstruction').value = '';
    showNotice('Tarea creada');
    await loadAll();
  } catch (err) { showNotice(err.message, 'error'); }
}
async function runTask(id) { try { await requestJSON(`/api/tasks/${id}/run`, {method: 'POST'}); showNotice('Tarea preparada para ejecutar'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function cancelTask(id) { try { await requestJSON(`/api/tasks/${id}/cancel`, {method: 'POST'}); showNotice('Tarea cancelada'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function deleteTask(id) { if (confirm('Eliminar tarea #' + id + '?')) { try { await requestJSON(`/api/tasks/${id}`, {method: 'DELETE'}); showNotice('Tarea eliminada'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } } }
async function updateAgent(name, payload) { try { await requestJSON(`/api/agents/${encodeURIComponent(name)}`, {method: 'PATCH', body: JSON.stringify(payload)}); showNotice('Agente actualizado'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function runAgent(name) { try { const result = await requestJSON(`/api/agents/${encodeURIComponent(name)}/run`, {method: 'POST'}); showNotice(result.message, result.ok ? 'ok' : 'error'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function refreshCodexContext() { try { await requestJSON('/api/codex/context/refresh', {method: 'POST'}); showNotice('Contexto Codex actualizado'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function createBackup() { try { const pass = document.getElementById('backupPassphrase').value; const result = await requestJSON('/api/backup', {method: 'POST', body: JSON.stringify({passphrase: pass || null})}); showNotice('Backup creado: ' + result.backup.path); document.getElementById('backupPassphrase').value = ''; await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function saveCodexNotes() { try { await requestJSON('/api/codex/notes', {method: 'POST', body: JSON.stringify({text: document.getElementById('codexNotes').value})}); showNotice('Notas Codex guardadas'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
loadAll().catch(err => { showNotice(err.message, 'error'); });
</script>
</body>
</html>
"""
