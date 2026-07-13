#!/usr/bin/env sh
set -eu

set -a
[ -r /config/segurai.env ] && . /config/segurai.env
set +a

mkdir -p "${CODEX_HOME:-/data/codex}" "${WORKSPACE:-/config/segurai-dev}" /config/data
cd "${WORKSPACE:-/config/segurai-dev}"

cat <<'EOF'
SegurAI terminal persistente

Esta terminal usa tmux. Puedes cerrar el navegador y volver sin perder la sesion.

Comandos utiles:
  cd "$WORKSPACE"
  cat /config/data/CODEX_CONTEXT.md
  tail -f /config/data/segurai_service.log
  tail -f /config/data/segurai_runtime.log
  python3 /app/segurai.py --help

EOF

exec tmux -u new-session -A -s segurai-terminal /bin/bash -l
