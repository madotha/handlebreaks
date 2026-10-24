#!/bin/zsh
# ─────────────────────────────────────────────────────────────────────────────
# encode.sh – works through the encoding queue with HandBrakeCLI.
#
# Started automatically by launchd as soon as a batch appears in queue/ (agent
# local.plex-encoding), or by the worker loop in the Docker container. Can also be
# run by hand in the terminal:   zsh ~/scripts/encoding/encode.sh
#
# Per file:
#   1. Read the original from the library
#   2. Encode it with your preset to <output>/.inprogress/<name>.mkv
#   3. Only when finished, move it to <output>/<name>.mkv
#      (so an aborted encode never counts as finished)
#   4. Log the result in logs/results.tsv (OK, FAIL, MISSING, EXISTS)
# The original in the library is never modified.
# ─────────────────────────────────────────────────────────────────────────────

set -u
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE="${0:A:h}"                                  # program folder, e.g. ~/scripts/encoding
DATA="${ENCODING_DATA_DIR:-$BASE}"               # config.env, queue/, logs/ … (Docker: /data)
QUEUE="$DATA/queue"; PROC="$DATA/processing"; DONE="$DATA/done"; LOGS="$DATA/logs"
mkdir -p "$QUEUE" "$PROC" "$DONE" "$LOGS"

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGS/encode.log"; }
result() { print -r -- "$(date '+%Y-%m-%d %H:%M')	$1	$2" >> "$LOGS/results.tsv"; }

# ── Settings from config.env (template: config.example.env) ──────────────────
if [[ ! -f "$DATA/config.env" ]]; then
  log "config.env is missing (template: config.example.env)"; sleep 300; exit 1
fi
source "$DATA/config.env"

# First existing path of a ":"-separated list, otherwise the first entry
first_existing() {
  local p; local -a ps=(${(s.:.)1})
  for p in $ps; do [[ -e "$p" ]] && { print -r -- "$p"; return }; done
  print -r -- "${ps[1]:-}"
}

MOVIES=$(first_existing "${MOVIES_DIR:-}")
SHOWS=$(first_existing "${SHOWS_DIR:-}")
OUT=$(first_existing "${ENCODED_DIR:-}")
PRESET_FILE="${PRESET_FILE:-$BASE/preset.json}"
PRESET_NAME="${PRESET_NAME:-}"                   # internal name of the preset
if [[ -z "$MOVIES" || -z "$SHOWS" || -z "$OUT" || -z "$PRESET_NAME" ]]; then
  log "config.env is incomplete (MOVIES_DIR, SHOWS_DIR, ENCODED_DIR, PRESET_NAME)"; sleep 300; exit 1
fi
mkdir -p "$OUT/.inprogress"

# ── Live status as an Obsidian note (via iCloud also visible on the phone) ───
# Turn on and off with STATUS_NOTE_ENABLED in config.env.
STATUS_NOTE_ENABLED="${STATUS_NOTE_ENABLED:-0}"
NOTE=$(first_existing "${NOTE_DIR:-}")
[[ -z "$NOTE" ]] && STATUS_NOTE_ENABLED=0
STATUS_NOTE="$NOTE/Encoding Status.md"
write_status() {
  (( STATUS_NOTE_ENABLED )) || return 0
  local body k="" i="" n="" t="" prog pct eta
  if [[ "${1:-}" == "idle" ]]; then
    body="No encode running. Last run finished: $(date '+%Y-%m-%d %H:%M')"
  else
    [[ -f "$LOGS/current.txt" ]] && IFS=$'\t' read -r k i n t < "$LOGS/current.txt"
    prog=$(tail -c 400 "$LOGS/progress.log" 2>/dev/null | tr '\r' '\n' | grep 'Encoding:' | tail -1)
    pct=$(print -r -- "$prog" | sed -nE 's/.*, ([0-9.]+) %.*/\1/p')
    eta=$(print -r -- "$prog" | sed -nE 's/.*ETA ([0-9]+h[0-9]+m[0-9]+s).*/\1/p')
    body="**Running:** file $i of $n
**File:** ${k:t}
**Progress:** ${pct:-starting …} %${eta:+ · $eta left}"
  fi
  local recent=$(tail -n 5 "$LOGS/results.tsv" 2>/dev/null | awk -F'\t' '{n=split($3,a,"/"); print "- " $1 " " $2 " · " a[n]}')
  print -r -- "# Encoding Status
> As of $(date '+%Y-%m-%d %H:%M') (updated every minute while encoding)

$body

## Recently finished
${recent:-*nothing yet*}
" > "$STATUS_NOTE.tmp" 2>/dev/null && mv "$STATUS_NOTE.tmp" "$STATUS_NOTE" 2>/dev/null
}

# ── Only one instance at a time ──────────────────────────────────────────────
LOCK="$DATA/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  oldpid=$(cat "$LOCK/pid" 2>/dev/null)
  if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
    exit 0                                       # already running
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0        # clean up a stale lock
fi
print $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"; rm -f "$LOGS/current.txt"' EXIT INT TERM

# ── Keep the Mac awake while encoding (macOS only) ───────────────────────────
command -v caffeinate >/dev/null && caffeinate -is -w $$ &

# ── Library reachable? Otherwise wait 5 minutes (launchd restarts afterwards) ─
if [[ ! -d "$MOVIES" || ! -d "$SHOWS" ]]; then
  log "Library not reachable, retrying in 5 minutes"
  sleep 300
  exit 1
fi
if ! command -v HandBrakeCLI >/dev/null; then
  log "HandBrakeCLI not found"; sleep 300; exit 1
fi

# ── Take over new batches ────────────────────────────────────────────────────
for f in "$QUEUE"/*.txt(N); do mv "$f" "$PROC/"; done

batches=("$PROC"/*.txt(N))
(( ${#batches} == 0 )) && exit 0

total=0
for b in $batches; do total=$(( total + $(grep -c . "$b") )); done
log "Starting: $total file(s)"

i=0
for b in $batches; do
  while IFS= read -r key || [[ -n "$key" ]]; do
    [[ -z "$key" ]] && continue
    i=$(( i + 1 ))
    case "$key" in
      Movies/*) src="$MOVIES/${key#Movies/}" ;;
      Shows/*)  src="$SHOWS/${key#Shows/}" ;;
      *) log "Unknown entry: $key"; continue ;;
    esac

    if [[ ! -f "$src" ]]; then
      log "Missing in library: $key"; result MISSING "$key"; continue
    fi

    stem="${${src:t}:r}"
    out="$OUT/$stem.mkv"
    tmp="$OUT/.inprogress/$stem.mkv"
    if [[ -e "$out" ]]; then
      log "Already exists: $out"; result EXISTS "$key"; continue
    fi

    print -r -- "$key	$i	$total	$(date +%s)" > "$LOGS/current.txt"
    log "[$i/$total] $key"
    rm -f "$tmp"
    # Progress (stdout) to progress.log, HandBrake log (stderr) to handbrake-last.log
    : > "$LOGS/progress.log"
    HandBrakeCLI --preset-import-file "$PRESET_FILE" -Z "$PRESET_NAME" \
                 -i "$src" -o "$tmp" \
                 > "$LOGS/progress.log" 2> "$LOGS/handbrake-last.log" < /dev/null &
    hbpid=$!
    while kill -0 $hbpid 2>/dev/null; do     # update the status note every minute
      write_status
      for s in {1..12}; do kill -0 $hbpid 2>/dev/null || break; sleep 5; done
    done
    wait $hbpid
    rc=$?
    if (( rc == 0 )) && [[ -s "$tmp" ]]; then
      mv "$tmp" "$out"
      log "  done: $out"; result OK "$key"
    else
      rm -f "$tmp"
      cp "$LOGS/handbrake-last.log" "$LOGS/fail-$(date +%Y%m%d-%H%M%S).log" 2>/dev/null
      log "  FAILED (exit code $rc): $key"; result FAIL "$key"
    fi
  done < "$b"
  mv "$b" "$DONE/"
done

log "Queue finished"
rm -f "$LOGS/current.txt"
write_status idle
