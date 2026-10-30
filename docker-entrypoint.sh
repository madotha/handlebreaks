#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# docker-entrypoint.sh – startup inside the Handlebreaks container
#
# 1. Creates config.env from the template on first start.
# 2. Starts the worker loop: when a batch is in queue/ or processing/ or a replace job
#    is waiting, it runs encode.sh (replaces launchd from the Mac installation).
# 3. Starts the web UI.
# 4. On docker stop (SIGTERM), stops the worker first: encode.sh ends the encode in
#    progress and puts the rest of its run back into the queue, then the web UI stops.
#    stop_grace_period in docker-compose.yml gives it the time for that.
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
  for f in "$DATA"/queue/*.txt "$DATA"/processing/*.txt "$DATA"/replace/*.json "$DATA"/replace-running/*.json; do
    [ -e "$f" ] && return 0
  done
  return 1
}

# Children run in the background and are waited for with `wait`, which a trapped signal
# interrupts at once (a foreground command would delay the trap until it ends).
worker() {
  stop=0; job=""
  trap 'stop=1; [ -n "$job" ] && kill -TERM "$job" 2>/dev/null' TERM
  while [ "$stop" = 0 ]; do
    if [ "$(worker_enabled)" = 1 ] && has_jobs; then
      zsh /app/encode.sh & job=$!
      while kill -0 "$job" 2>/dev/null; do wait "$job" || true; done
      job=""
    fi
    [ "$stop" = 0 ] && { sleep 30 & job=$!; wait "$job" || true; job=""; }
  done
}

worker & worker_pid=$!
python3 /app/webui.py & web_pid=$!

stopping=0
trap 'stopping=1' TERM INT
wait "$web_pid" || true                          # returns on a signal or when the web UI ends
kill -TERM "$worker_pid" 2>/dev/null             # the worker first: it requeues its run
while kill -0 "$worker_pid" 2>/dev/null; do wait "$worker_pid" || true; done
if [ "$stopping" = 1 ]; then
  kill -TERM "$web_pid" 2>/dev/null
  while kill -0 "$web_pid" 2>/dev/null; do wait "$web_pid" || true; done
  exit 0
fi
exit 1                                           # the web UI ended by itself: let Docker restart the container
