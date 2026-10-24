#!/bin/zsh
# ─────────────────────────────────────────────────────────────────────────────
# encode.sh – arbeitet die Encoding-Warteschlange mit HandBrakeCLI ab.
#
# Wird automatisch von launchd gestartet, sobald im Ordner queue/ ein Auftrag
# liegt (Agent local.plex-encoding). Kann auch von Hand im Terminal gestartet
# werden:   zsh ~/scripts/encoding/encode.sh
#
# Ablauf pro Datei:
#   1. Original vom NAS lesen
#   2. Mit deinem Preset nach _ENCODED/.inprogress/<Name>.mkv encodieren
#   3. Erst wenn alles fertig ist, nach _ENCODED/<Name>.mkv verschieben
#      (so gilt eine abgebrochene Datei nie als "fertig")
#   4. Ergebnis in logs/results.tsv eintragen (OK, FAIL, MISSING, EXISTS)
# Das Original auf dem NAS wird nie verändert.
# ─────────────────────────────────────────────────────────────────────────────

set -u
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE="${0:A:h}"                                  # ~/scripts/encoding
QUEUE="$BASE/queue"; PROC="$BASE/processing"; DONE="$BASE/done"; LOGS="$BASE/logs"
mkdir -p "$QUEUE" "$PROC" "$DONE" "$LOGS"

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGS/encode.log"; }
result() { print -r -- "$(date '+%Y-%m-%d %H:%M')	$1	$2" >> "$LOGS/results.tsv"; }

# ── Einstellungen aus config.env (Vorlage: config.example.env) ───────────────
if [[ ! -f "$BASE/config.env" ]]; then
  log "config.env fehlt (Vorlage: config.example.env)"; sleep 300; exit 1
fi
source "$BASE/config.env"

# Erster vorhandener Pfad aus einer ":"-getrennten Liste, sonst der erste Eintrag
first_existing() {
  local p; local -a ps=(${(s.:.)1})
  for p in $ps; do [[ -e "$p" ]] && { print -r -- "$p"; return }; done
  print -r -- "${ps[1]:-}"
}

FILME=$(first_existing "${FILME_DIR:-}")
SERIEN=$(first_existing "${SERIEN_DIR:-}")
OUT=$(first_existing "${ENCODED_DIR:-}")
PRESET_FILE="${PRESET_FILE:-$BASE/preset.json}"
PRESET_NAME="${PRESET_NAME:-}"                   # interner Name im Preset
if [[ -z "$FILME" || -z "$SERIEN" || -z "$OUT" || -z "$PRESET_NAME" ]]; then
  log "config.env unvollständig (FILME_DIR, SERIEN_DIR, ENCODED_DIR, PRESET_NAME)"; sleep 300; exit 1
fi
mkdir -p "$OUT/.inprogress"

# ── Live-Status als Obsidian-Notiz (über iCloud auch auf dem Handy sichtbar) ─
# Ein- und ausschalten mit STATUS_NOTE_ENABLED in config.env.
STATUS_NOTE_ENABLED="${STATUS_NOTE_ENABLED:-0}"
NOTE=$(first_existing "${NOTE_DIR:-}")
[[ -z "$NOTE" ]] && STATUS_NOTE_ENABLED=0
STATUS_NOTE="$NOTE/Encoding Status.md"
write_status() {
  (( STATUS_NOTE_ENABLED )) || return 0
  local body k="" i="" n="" t="" prog pct eta
  if [[ "${1:-}" == "idle" ]]; then
    body="Kein Encoding aktiv. Letzter Lauf beendet: $(date '+%d.%m.%Y %H:%M')"
  else
    [[ -f "$LOGS/current.txt" ]] && IFS=$'\t' read -r k i n t < "$LOGS/current.txt"
    prog=$(tail -c 400 "$LOGS/progress.log" 2>/dev/null | tr '\r' '\n' | grep 'Encoding:' | tail -1)
    pct=$(print -r -- "$prog" | sed -nE 's/.*, ([0-9.]+) %.*/\1/p')
    eta=$(print -r -- "$prog" | sed -nE 's/.*ETA ([0-9]+h[0-9]+m[0-9]+s).*/\1/p')
    body="**Läuft:** Datei $i von $n
**Datei:** ${k:t}
**Fortschritt:** ${pct:-startet …} %${eta:+ · Restzeit $eta}"
  fi
  local recent=$(tail -n 5 "$LOGS/results.tsv" 2>/dev/null | awk -F'\t' '{n=split($3,a,"/"); print "- " $1 " " $2 " · " a[n]}')
  print -r -- "# Encoding Status
> Stand: $(date '+%d.%m.%Y %H:%M') (wird während des Encodings jede Minute aktualisiert)

$body

## Zuletzt abgeschlossen
${recent:-*noch nichts*}
" > "$STATUS_NOTE.tmp" 2>/dev/null && mv "$STATUS_NOTE.tmp" "$STATUS_NOTE" 2>/dev/null
}

# ── Nur eine Instanz gleichzeitig ────────────────────────────────────────────
LOCK="$BASE/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  oldpid=$(cat "$LOCK/pid" 2>/dev/null)
  if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
    exit 0                                       # läuft schon
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0        # verwaiste Sperre aufräumen
fi
print $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"; rm -f "$LOGS/current.txt"' EXIT INT TERM

# ── Mac während des Encodings wach halten ────────────────────────────────────
caffeinate -is -w $$ &

# ── NAS vorhanden? Sonst 5 Minuten warten (launchd startet danach neu) ───────
if [[ ! -d "$FILME" || ! -d "$SERIEN" ]]; then
  log "NAS nicht erreichbar, neuer Versuch in 5 Minuten"
  sleep 300
  exit 1
fi
if ! command -v HandBrakeCLI >/dev/null; then
  log "HandBrakeCLI nicht gefunden"; sleep 300; exit 1
fi

# ── Neue Aufträge übernehmen ─────────────────────────────────────────────────
for f in "$QUEUE"/*.txt(N); do mv "$f" "$PROC/"; done

batches=("$PROC"/*.txt(N))
(( ${#batches} == 0 )) && exit 0

total=0
for b in $batches; do total=$(( total + $(grep -c . "$b") )); done
log "Starte: $total Datei(en)"

i=0
for b in $batches; do
  while IFS= read -r key || [[ -n "$key" ]]; do
    [[ -z "$key" ]] && continue
    i=$(( i + 1 ))
    case "$key" in
      Filme/*)  src="$FILME/${key#Filme/}" ;;
      Serien/*) src="$SERIEN/${key#Serien/}" ;;
      *) log "Unbekannter Eintrag: $key"; continue ;;
    esac

    if [[ ! -f "$src" ]]; then
      log "Fehlt auf NAS: $key"; result MISSING "$key"; continue
    fi

    stem="${${src:t}:r}"
    out="$OUT/$stem.mkv"
    tmp="$OUT/.inprogress/$stem.mkv"
    if [[ -e "$out" ]]; then
      log "Schon vorhanden: $out"; result EXISTS "$key"; continue
    fi

    print -r -- "$key	$i	$total	$(date +%s)" > "$LOGS/current.txt"
    log "[$i/$total] $key"
    rm -f "$tmp"
    # Fortschritt (stdout) nach progress.log, HandBrake-Protokoll (stderr) nach handbrake-last.log
    : > "$LOGS/progress.log"
    HandBrakeCLI --preset-import-file "$PRESET_FILE" -Z "$PRESET_NAME" \
                 -i "$src" -o "$tmp" \
                 > "$LOGS/progress.log" 2> "$LOGS/handbrake-last.log" < /dev/null &
    hbpid=$!
    while kill -0 $hbpid 2>/dev/null; do     # Status jede Minute aktualisieren
      write_status
      for s in {1..12}; do kill -0 $hbpid 2>/dev/null || break; sleep 5; done
    done
    wait $hbpid
    rc=$?
    if (( rc == 0 )) && [[ -s "$tmp" ]]; then
      mv "$tmp" "$out"
      log "  fertig: $out"; result OK "$key"
    else
      rm -f "$tmp"
      cp "$LOGS/handbrake-last.log" "$LOGS/fail-$(date +%Y%m%d-%H%M%S).log" 2>/dev/null
      log "  FEHLER (Code $rc): $key"; result FAIL "$key"
    fi
  done < "$b"
  mv "$b" "$DONE/"
done

log "Warteschlange abgearbeitet"
rm -f "$LOGS/current.txt"
write_status idle
