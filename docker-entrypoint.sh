#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# docker-entrypoint.sh – startup inside the container
#
# 1. Creates config.env from the template on first start.
# 2. Starts the worker loop: when a batch is in queue/ or processing/, it runs
#    encode.sh (replaces launchd from the Mac installation).
# 3. Starts the web UI.
#
# Whether the container encodes is controlled by WORKER_ENABLED in config.env
# (web UI: Settings → Worker). If WORKER_ENABLED is set as an environment variable
# (docker-compose.hybrid.yml), that value wins and config.env is ignored for it.
# ─────────────────────────────────────────────────────────────────────────────
set -eu
DATA="${ENCODING_DATA_DIR:-/data}"
mkdir -p "$DATA"
[ -f "$DATA/config.env" ] || cp /app/config.example.env "$DATA/config.env"

worker_enabled() {
  if [ -n "${WORKER_ENABLED:-}" ]; then
    echo "$WORKER_ENABLED"
  else
    ( . "$DATA/config.env" 2>/dev/null; echo "${WORKER_ENABLED:-1}" )
  fi
}

has_jobs() {
  for f in "$DATA"/queue/*.txt "$DATA"/processing/*.txt "$DATA"/replace/*.json; do
    [ -e "$f" ] && return 0
  done
  return 1
}

worker() {
  while :; do
    if [ "$(worker_enabled)" = 1 ] && has_jobs; then
      zsh /app/encode.sh || true
    fi
    sleep 30
  done
}

worker &
exec python3 /app/webui.py
