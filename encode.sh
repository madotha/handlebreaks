#!/bin/zsh
# ─────────────────────────────────────────────────────────────────────────────
# encode.sh – the Handlebreaks worker: works through the encoding queue with HandBrakeCLI.
#
# Started automatically by launchd as soon as a batch appears in queue/ (agent
# local.plex-encoding), or by the worker loop in the Docker container. Can also be
# run by hand in the terminal:   zsh ~/scripts/encoding/encode.sh
#
# Per file:
#   1. Read the original from the library
#   2. Encode it to <output>/.inprogress/<name>.<ext> with the preset chosen when
#      queueing, otherwise the share's preset (SHARE_N_PRESET), otherwise DEFAULT_PRESET
#   3. Only when finished, move it to <output>/<name>.mkv
#      (so an aborted encode never counts as finished)
#   4. Log the result in logs/results.tsv (OK, FAIL, MISSING, EXISTS, STOPPED)
#
# The web UI can stop the running encode (stop.txt, checked every 5 s): "skip" goes on
# with the next file, "all" puts the rest of the run back into the queue and pauses it
# (paused) until "Resume".
# It also carries out replace jobs from the web UI (replace/*.json: move a finished
# encode into the library in place of the original), at the start and every minute
# while encoding, so they don't wait for a long encode.
# The original in the library is never modified.
# ─────────────────────────────────────────────────────────────────────────────

set -u
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE="${0:A:h}"                                  # program folder, e.g. ~/scripts/encoding
DATA="${ENCODING_DATA_DIR:-$BASE}"               # config.env, queue/, logs/ … (Docker: /data)
QUEUE="$DATA/queue"; PROC="$DATA/processing"; DONE="$DATA/done"; LOGS="$DATA/logs"
CANCELLED="$DATA/cancelled.txt"   # "batch<TAB>key" removed from this run in the web UI
STOPFILE="$DATA/stop.txt"         # "skip|all<TAB>key": stop the running encode (web UI)
PAUSED="$DATA/paused"             # queue paused by "Stop all"; removed by "Resume"
mkdir -p "$QUEUE" "$PROC" "$DONE" "$LOGS"

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGS/encode.log"; }

# Move or delete files on the library/output share. Started by launchd on macOS, /bin/mv
# and /bin/rm may get "Operation not permitted" on network shares while Python's file
# functions work, so all file operations on the share go through Python.
#   fs mv SRC DST     fs rm FILE…
fs() {
  python3 -c 'import os, shutil, sys
op, args = sys.argv[1], sys.argv[2:]
try:
    if op == "mv":
        shutil.move(args[0], args[1])
    else:
        for path in args:
            if os.path.lexists(path):
                os.unlink(path)
except OSError as e:
    sys.exit(f"{op}: {e.strerror}: {e.filename}")' "$@"
}
# HandBrake's own verdict from its log: "work result = 0", "Encode done!" and a non-empty
# output file. Used when the shell reports a non-zero exit code for an encode that finished
# (a lost or mixed-up status of the background job, seen as exit code 19).
hb_finished() {
  [[ -s "$1" && -r "$LOGS/handbrake-last.log" ]] || return 1
  grep -q 'work result = 0' "$LOGS/handbrake-last.log" &&
    grep -q 'Encode done!' "$LOGS/handbrake-last.log"
}
# True if the share of this entry or the output folder is gone (e.g. unmounted while
# encoding). Then a missing source or a failed encode isn't the file's fault.
offline() { ! reachable "${ROOTS[$1]}" || [[ ! -d "$OUT" ]]; }
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

# Shares: SHARE_1, SHARE_2, … Each one is a key prefix ("share1", …) mapped to its
# folder and its preset (SHARE_N_PRESET, empty = DEFAULT_PRESET).
typeset -A ROOTS SHARE_PRESET
for name in ${(k)parameters[(I)SHARE_<1->]}; do
  [[ -n "${(P)name}" ]] || continue
  ROOTS[share${name#SHARE_}]=$(first_existing "${(P)name}")
  pvar="${name}_PRESET"; SHARE_PRESET[share${name#SHARE_}]="${(P)pvar:-}"
done
OUT=$(first_existing "${ENCODED_DIR:-}")
PRESET_DIR="$DATA/presets"
DEFAULT_PRESET="${DEFAULT_PRESET:-}"
if (( ${+MOVIES_DIR} || ${+SHOWS_DIR} )) && (( ${#ROOTS} == 0 )); then
  log "config.env is from version 2.x: run 'python3 encoding.py status' or open the web UI once to migrate it"
  sleep 300; exit 1
fi
if (( ${#ROOTS} == 0 )) || [[ -z "$OUT" || -z "$DEFAULT_PRESET" ]]; then
  log "config.env is incomplete (SHARE_1, ENCODED_DIR, DEFAULT_PRESET)"; sleep 300; exit 1
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
> As of $(date '+%Y-%m-%d %H:%M') (updated every minute by Handlebreaks while encoding)

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
: > "$LOCK/stoppable"                            # tells the web UI that this worker reads stop.txt
trap 'rm -rf "$LOCK"; rm -f "$LOGS/current.txt" "$LOGS/run.txt"' EXIT INT TERM

# ── Keep the Mac awake while encoding (macOS only) ───────────────────────────
if command -v caffeinate >/dev/null; then caffeinate -is -w $$ & caf=$!; fi

# A share counts as reachable only if its folder has something in it: an unmounted share
# usually leaves an empty mount point (or no folder at all) behind.
reachable() { [[ -n "$1" && -d "$1" ]] && [[ -n $(print -r -- "$1"/*(DNY1)) ]]; }

# ── Library reachable? Otherwise wait 5 minutes (launchd restarts afterwards) ─
for prefix root in ${(kv)ROOTS}; do
  if ! reachable "$root"; then
    log "Library not reachable ($prefix: $root), retrying in 5 minutes"
    sleep 300
    exit 1
  fi
done
if ! command -v HandBrakeCLI >/dev/null; then
  log "HandBrakeCLI not found"; sleep 300; exit 1
fi

# ── Replace jobs (python3 encoding.py replace-jobs) ──────────────────────────
replace_jobs() {
  [[ -n "$(print -r -- "$DATA"/replace/*.json(N))" ]] || return 0
  python3 "$BASE/encoding.py" replace-jobs >> "$LOGS/encode.log" 2>&1
}
replace_jobs

# What this worker's HandBrakeCLI supports, for the preset editor of a web UI that runs
# elsewhere (e.g. in Docker while the Mac encodes). Only rewritten when HandBrakeCLI changed
# (that takes ~20 s once), so it runs in the background and encoding doesn't wait for it.
python3 "$BASE/encoding.py" handbrake-caps >> "$LOGS/encode.log" 2>&1 &!

# ── Paused by "Stop all" in the web UI: wait for "Resume" ────────────────────
# Replace jobs still run. After 10 minutes the worker exits; launchd or the Docker
# loop start it again as long as batches are waiting.
if [[ -f "$PAUSED" && -n "$(print -r -- "$QUEUE"/*.txt(N) "$PROC"/*.txt(N))" ]]; then
  [[ -n "${caf:-}" ]] && kill $caf 2>/dev/null   # let the Mac sleep while paused
  for s in {1..60}; do
    [[ -f "$PAUSED" ]] || break
    (( s % 6 )) || replace_jobs
    sleep 10
  done
  [[ -f "$PAUSED" ]] && exit 0
  log "Queue resumed"
  if command -v caffeinate >/dev/null; then caffeinate -is -w $$ & caf=$!; fi
fi

# Prints the mode ("skip" or "all") if the web UI asked to stop the encode of key $1
stop_request() {
  [[ -f "$STOPFILE" ]] || return 1
  local mode k
  IFS=$'\t' read -r mode k < "$STOPFILE"
  if [[ "$k" != "$1" ]]; then rm -f "$STOPFILE"; return 1; fi   # meant for a file that's done
  print -r -- "$mode"
}

# The library or output folder went offline during the run: put the current entry and
# everything after it back into the queue (no result, no pause), end the run and let
# launchd (or the Docker loop) retry until it's reachable again.
went_offline() {
  local n
  n=$(python3 "$BASE/encoding.py" stop-rest $(( i - 1 )) --no-pause 2>> "$LOGS/encode.log")
  log "Library or output folder went offline: ${n:-0} file(s) back in the queue, retrying when it's reachable"
  stopped_all=1
}

# ── Take over new batches ────────────────────────────────────────────────────
for f in "$QUEUE"/*.txt(N); do mv "$f" "$PROC/"; done

batches=("$PROC"/*.txt(N))
(( ${#batches} == 0 )) && exit 0

total=0
for b in $batches; do total=$(( total + $(grep -c . "$b") )); done
# The run's batches in order. Entries are numbered across all of them (current.txt), and a
# finished batch moves to done/ at once, so the web UI needs this list to tell which entry
# is which (and "Stop all" which ones are left).
print -rl -- ${batches:t} > "$LOGS/run.txt"
log "Starting: $total file(s)"

i=0
stopped_all=0
for b in $batches; do
  # A batch line is "key" or "key<TAB>preset id" (preset chosen when queueing)
  while IFS=$'\t' read -r key override || [[ -n "$key" ]]; do
    [[ -z "$key" ]] && continue
    i=$(( i + 1 ))
    if [[ -f "$CANCELLED" ]] && grep -qxF -- "${b:t}	$key" "$CANCELLED"; then
      log "Removed from the queue, skipped: $key"; continue
    fi
    prefix="${key%%/*}"
    if [[ "$key" != */* || -z "${ROOTS[$prefix]:-}" ]]; then
      log "Unknown entry: $key"; continue
    fi
    src="${ROOTS[$prefix]}/${key#*/}"

    if [[ ! -f "$src" ]]; then
      if offline "$prefix"; then went_offline; break 2; fi
      log "Missing in library: $key"; result MISSING "$key"; continue
    fi

    preset_id="${override:-${SHARE_PRESET[$prefix]:-$DEFAULT_PRESET}}"
    preset_file="$PRESET_DIR/$preset_id.json"
    # Name, container and quality ("rf20" or "avg2000", same as presets.quality_tag) of the
    # preset (HandBrake export JSON with one preset)
    preset_info=$(python3 -c 'import json, sys
p = json.load(open(sys.argv[1]))["PresetList"][0]
print(p["PresetName"]); print({"av_mp4": "mp4", "av_webm": "webm"}.get(p.get("FileFormat"), "mkv"))
print("avg%d" % int(p.get("VideoAvgBitrate") or 0) if p.get("VideoQualityType", 2) == 1
      else "rf" + format(float(p.get("VideoQualitySlider") or 0), "g"))' "$preset_file" 2>/dev/null)
    if [[ -z "$preset_info" ]]; then
      log "Preset '$preset_id' not found or unreadable: $key"; result FAIL "$key"; continue
    fi
    info=("${(@f)preset_info}")
    preset_name=$info[1]; ext=$info[2]; quality=$info[3]

    stem="${${src:t}:r}"
    out="$OUT/$stem.$ext"
    tmp="$OUT/.inprogress/$stem.$ext"
    if [[ -e "$out" ]]; then
      log "Already exists: $out"; result EXISTS "$key"; continue
    fi

    print -r -- "$key	$i	$total	$(date +%s)" > "$LOGS/current.txt"
    log "[$i/$total] $key (preset: $preset_name)"
    fs rm "$tmp"
    # Progress (stdout) to progress.log, HandBrake log (stderr) to handbrake-last.log
    : > "$LOGS/progress.log"
    HandBrakeCLI --preset-import-file "$preset_file" -Z "$preset_name" \
                 -i "$src" -o "$tmp" \
                 > "$LOGS/progress.log" 2> "$LOGS/handbrake-last.log" < /dev/null &
    hbpid=$!
    stop_mode=""
    while kill -0 $hbpid 2>/dev/null; do     # every minute: status note, replace jobs
      write_status
      replace_jobs
      for s in {1..12}; do                    # every 5 s: still running, stop requested?
        kill -0 $hbpid 2>/dev/null || break
        if stop_mode=$(stop_request "$key"); then break 2; fi
        sleep 5
      done
    done
    if [[ -n "$stop_mode" ]]; then           # stopped in the web UI
      kill $hbpid 2>/dev/null; wait $hbpid 2>/dev/null
      rm -f "$STOPFILE"
      fs rm "$tmp"
      log "  STOPPED in the web UI: $key"; result STOPPED "$key"
      if [[ "$stop_mode" == all ]]; then
        n=$(python3 "$BASE/encoding.py" stop-rest "$i" 2>> "$LOGS/encode.log")
        log "Stopped all: ${n:-0} remaining file(s) back in the queue, paused until Resume"
        stopped_all=1
        break 2
      fi
      continue
    fi
    wait $hbpid
    rc=$?
    if (( rc != 0 )) && hb_finished "$tmp"; then
      log "  shell reported exit code $rc, but HandBrake reports success: file kept"
      rc=0
    fi
    if (( rc == 0 )) && [[ -s "$tmp" ]]; then
      # Move the finished file out of .inprogress. On a NAS the move can fail for a
      # moment (e.g. while its media indexer reads the new file), so retry a few times.
      moved=0
      for attempt in {1..6}; do
        if err=$(fs mv "$tmp" "$out" 2>&1); then moved=1; break; fi
        log "  move failed (attempt $attempt/6, retrying in 10 s): $err"
        sleep 10
      done
      if (( moved )); then
        log "  done: $out"; result OK "$key"
        # which preset and quality made this encode (for the name suggested by Replace…)
        print -r -- "$(date '+%Y-%m-%d %H:%M')	$key	$preset_id	$quality" >> "$LOGS/encodes.tsv"
      else
        log "  FAILED: encoded, but the file couldn't be moved; it stays in $tmp: $key"; result FAIL "$key"
      fi
    elif offline "$prefix"; then             # failed because the share or output folder went away
      fs rm "$tmp" 2>/dev/null
      log "  interrupted (exit code $rc): $key"
      went_offline; break 2
    else
      fs rm "$tmp"
      cp "$LOGS/handbrake-last.log" "$LOGS/fail-$(date +%Y%m%d-%H%M%S).log" 2>/dev/null
      log "  FAILED (exit code $rc): $key"; result FAIL "$key"
    fi
  done < "$b"
  mv "$b" "$DONE/"
done

(( stopped_all )) || log "Queue finished"
rm -f "$LOGS/current.txt" "$LOGS/run.txt" "$CANCELLED" "$STOPFILE"
write_status idle
