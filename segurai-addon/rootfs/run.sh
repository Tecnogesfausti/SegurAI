#!/usr/bin/env sh
set -eu

CONFIG_DIR=/config
APP_DIR=/app
OPTIONS=/data/options.json
ENV_FILE="${CONFIG_DIR}/segurai.env"
SERVICE_LOG="${CONFIG_DIR}/data/segurai_service.log"

mkdir -p "${CONFIG_DIR}" "${CONFIG_DIR}/agents" "${CONFIG_DIR}/data"

python3 - <<'PY'
import json
import os
from pathlib import Path

options_path = Path('/data/options.json')
options = json.loads(options_path.read_text(encoding='utf-8')) if options_path.exists() else {}
config_dir = Path('/config')
env_path = config_dir / 'segurai.env'

def value(name, default=''):
    return options.get(name, default)

lines = [
    f"OPENROUTER_API_KEY={value('openrouter_api_key', '')}",
    f"SEGURAI_MODEL_ROUTES=/app/{value('model_routes', 'model_routes.yaml')}",
    "SEGURAI_AGENTS_DIR=/config/agents",
    "SEGURAI_DB=/config/data/segurai_memory.sqlite3",
    "SEGURAI_AGENT_CONFIG=/config/data/agent_config.json",
    "SEGURAI_LOG_FILE=/config/data/segurai_runtime.log",
    f"SEGURAI_POLL_SECONDS={value('poll_seconds', 300)}",
    f"SEGURAI_SENSOR_PROMPT={value('sensor_prompt', '')}",
    f"SEGURAI_FS_ROOTS={value('fs_roots', '/config,/share')}",
    "HA_MCP_URL=http://supervisor/core/api/mcp",
    "HOME_ASSISTANT_URL=http://supervisor/core",
    f"HA_TOKEN={os.environ.get('SUPERVISOR_TOKEN', '')}",
    f"HOME_ASSISTANT_TOKEN={os.environ.get('SUPERVISOR_TOKEN', '')}",
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

if [ "$WEB_ONLY" = "true" ]; then
  exec python3 -m uvicorn segurai_web:app --host 0.0.0.0 --port 8099
fi

if [ "$AGENT_SERVICE_ENABLED" = "true" ]; then
  exec tail -F "$SERVICE_LOG"
fi

exec python3 segurai.py $ARGS
