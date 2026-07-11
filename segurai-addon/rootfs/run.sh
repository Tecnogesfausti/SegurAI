#!/usr/bin/env sh
set -eu

CONFIG_DIR=/config
APP_DIR=/app
OPTIONS=/data/options.json
ENV_FILE="${CONFIG_DIR}/segurai.env"

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
    "SEGURAI_LOG_FILE=/config/data/segurai_runtime.log",
    f"SEGURAI_POLL_SECONDS={value('poll_seconds', 300)}",
    f"SEGURAI_SENSOR_PROMPT={value('sensor_prompt', '')}",
    f"SEGURAI_FS_ROOTS={value('fs_roots', '/config,/share')}",
    "HA_MCP_URL=http://supervisor/core/api/mcp",
    f"HA_TOKEN={os.environ.get('SUPERVISOR_TOKEN', '')}",
]
env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
env_path.chmod(0o600)
PY

cp -n "${APP_DIR}/agents/monitor_temperatura.py" "${CONFIG_DIR}/agents/monitor_temperatura.py" || true

set -a
. "${ENV_FILE}"
set +a

WEB_ONLY=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print('true' if options.get('web_only', True) else 'false')
PY
)

if [ "$WEB_ONLY" = "true" ]; then
  exec python3 -m uvicorn segurai_web:app --host 0.0.0.0 --port 8099
fi

ARGS=""
NO_SENSOR_LOOP=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print('true' if options.get('no_sensor_loop', False) else 'false')
PY
)
ALLOW_ACTIONS=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print('true' if options.get('allow_actions_without_confirmation', False) else 'false')
PY
)

if [ "$NO_SENSOR_LOOP" = "true" ]; then
  ARGS="$ARGS --no-sensor-loop"
fi
if [ "$ALLOW_ACTIONS" = "true" ]; then
  ARGS="$ARGS --allow-actions-without-confirmation"
fi

exec python3 segurai.py $ARGS
