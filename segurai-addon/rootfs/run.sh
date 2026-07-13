#!/usr/bin/env sh
set -eu

CONFIG_DIR=/config
APP_DIR=/app
OPTIONS=/data/options.json
ENV_FILE="${CONFIG_DIR}/segurai.env"
SERVICE_LOG="${CONFIG_DIR}/data/segurai_service.log"
TERMINAL_LOG="${CONFIG_DIR}/data/segurai_terminal.log"

mkdir -p "${CONFIG_DIR}" "${CONFIG_DIR}/agents" "${CONFIG_DIR}/data"

python3 - <<'PY'
import json
import os
import shlex
from pathlib import Path

options_path = Path('/data/options.json')
options = json.loads(options_path.read_text(encoding='utf-8')) if options_path.exists() else {}
config_dir = Path('/config')
env_path = config_dir / 'segurai.env'

def value(name, default=''):
    item = options.get(name, default)
    return item if item is not None else default

def env_line(name, val):
    return f"{name}={shlex.quote(str(val))}"

ha_token = value('home_assistant_token') or value('ha_long_lived_token') or os.environ.get('SUPERVISOR_TOKEN', '')
mcp_server_url = value('mcp_server_url') or 'http://supervisor/core/api/mcp'
mcp_server_api_key = value('mcp_server_api_key') or os.environ.get('SUPERVISOR_TOKEN', '') or ha_token
codex_model = value('codex_model', 'gpt-5.3-codex')
codex_home = value('codex_home', '/data/codex')
workspace = value('workspace', '/config/segurai-dev')

lines = [
    env_line("OPENROUTER_API_KEY", value('openrouter_api_key', '')),
    env_line("SEGURAI_MODEL_ROUTES", f"/app/{value('model_routes', 'model_routes.yaml')}"),
    env_line("SEGURAI_AGENTS_DIR", "/config/agents"),
    env_line("SEGURAI_DB", "/config/data/segurai_memory.sqlite3"),
    env_line("SEGURAI_AGENT_CONFIG", "/config/data/agent_config.json"),
    env_line("SEGURAI_CODEX_CONTEXT", "/config/data/CODEX_CONTEXT.md"),
    env_line("SEGURAI_CODEX_NOTES", "/config/data/CODEX_NOTES.md"),
    env_line("SEGURAI_BACKUP_DIR", "/config/data/backups"),
    env_line("SEGURAI_BACKUP_KEY", value('backup_key')),
    env_line("CODEX_MODEL", codex_model),
    env_line("CODEX_HOME", codex_home),
    env_line("WORKSPACE", workspace),
    env_line("SEGURAI_WORKSPACE", workspace),
    env_line("SEGURAI_LOG_FILE", "/config/data/segurai_runtime.log"),
    env_line("SEGURAI_POLL_SECONDS", value('poll_seconds', 300)),
    env_line("SEGURAI_SENSOR_PROMPT", value('sensor_prompt', '')),
    env_line("SEGURAI_FS_ROOTS", value('fs_roots', '/config,/share')),
    env_line("HA_MCP_URL", mcp_server_url),
    env_line("MCP_SERVER_URL", mcp_server_url),
    env_line("MCP_SERVER_API_KEY", mcp_server_api_key),
    env_line("MCP_AUTH_TOKEN", mcp_server_api_key),
    env_line("HOME_ASSISTANT_URL", "http://supervisor/core"),
    env_line("HA_TOKEN", ha_token),
    env_line("HOME_ASSISTANT_TOKEN", ha_token),
    env_line("HA_LONG_LIVED_TOKEN", value('ha_long_lived_token')),
]
env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
env_path.chmod(0o600)
PY

for file in "${APP_DIR}"/agents/*.py; do
  cp -n "$file" "${CONFIG_DIR}/agents/$(basename "$file")" || true
done
mkdir -p "${CONFIG_DIR}/agents/pending"

set -a
. "${ENV_FILE}"
set +a

mkdir -p "$CODEX_HOME" "$WORKSPACE"

read_bool() {
  key="$1"
  fallback="$2"
  python3 - "$key" "$fallback" <<'PY'
import json
import sys
from pathlib import Path
key, fallback = sys.argv[1], sys.argv[2]
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print('true' if options.get(key, fallback == 'true') else 'false')
PY
}

WEB_ONLY=$(read_bool web_only true)
AGENT_SERVICE_ENABLED=$(read_bool agent_service_enabled true)
TERMINAL_ENABLED=$(read_bool terminal_enabled true)
TERMINAL_PORT=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print(int(options.get('terminal_port', 8098)))
PY
)
TERMINAL_USERNAME=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print(options.get('terminal_username') or 'segurai')
PY
)
TERMINAL_PASSWORD=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print(options.get('terminal_password') or 'segurai')
PY
)
NO_SENSOR_LOOP=$(read_bool no_sensor_loop false)
ALLOW_ACTIONS=$(read_bool allow_actions_without_confirmation false)

ARGS=""
if [ "$NO_SENSOR_LOOP" = "true" ]; then
  ARGS="$ARGS --no-sensor-loop"
fi
if [ "$ALLOW_ACTIONS" = "true" ]; then
  ARGS="$ARGS --allow-actions-without-confirmation"
fi

if [ "$AGENT_SERVICE_ENABLED" = "true" ]; then
  echo "[SegurAI add-on] starting SegurAI service" | tee -a "$SERVICE_LOG"
  python3 segurai.py --service $ARGS >>"$SERVICE_LOG" 2>&1 &
fi

if [ "$TERMINAL_ENABLED" = "true" ]; then
  echo "[SegurAI add-on] starting tmux terminal on ${TERMINAL_PORT}" | tee -a "$TERMINAL_LOG"
  ttyd \
    --port "$TERMINAL_PORT" \
    --interface 0.0.0.0 \
    --credential "${TERMINAL_USERNAME}:${TERMINAL_PASSWORD}" \
    --writable \
    --terminal-type xterm-256color \
    --client-option titleFixed="SegurAI Terminal" \
    --client-option cursorBlink=true \
    --client-option cursorStyle=bar \
    --client-option disableLeaveAlert=true \
    /usr/local/bin/segurai-terminal.sh >>"$TERMINAL_LOG" 2>&1 &
fi

if [ "$WEB_ONLY" = "true" ]; then
  exec python3 -m uvicorn segurai_web:app --host 0.0.0.0 --port 8099
fi

if [ "$AGENT_SERVICE_ENABLED" = "true" ]; then
  exec tail -F "$SERVICE_LOG"
fi

exec python3 segurai.py $ARGS
