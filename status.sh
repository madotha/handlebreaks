#!/bin/zsh
# ─────────────────────────────────────────────────────────────────────────────
# status.sh – Live-Anzeige des Encodings im Terminal (wie die HandBrake-Warteschlange)
# Aufruf:  zsh ~/scripts/encoding/status.sh      Beenden: Ctrl + C
# Das Beenden der Anzeige stoppt das Encoding NICHT.
# ─────────────────────────────────────────────────────────────────────────────
BASE="${0:A:h}"; LOGS="$BASE/logs"

bar() {  # Fortschrittsbalken, 40 Zeichen
  local p=${1%.*}; [[ -z $p ]] && p=0
  local f=$(( p * 40 / 100 ))
  printf '[%s%s]' "${(l:$f::█:)}" "${(l:$((40-f))::░:)}"
}

while true; do
  clear
  print "═══════════════  HandBrake Encoding  ═══════════════"
  print "Stand: $(date '+%H:%M:%S')\n"

  if [[ -f "$LOGS/current.txt" ]] && pgrep -x HandBrakeCLI >/dev/null; then
    IFS=$'\t' read -r key i n t < "$LOGS/current.txt"
    prog=$(tail -c 400 "$LOGS/progress.log" 2>/dev/null | tr '\r' '\n' | grep 'Encoding:' | tail -1)
    pct=$(print -r -- "$prog" | sed -nE 's/.*, ([0-9.]+) %.*/\1/p')
    fps=$(print -r -- "$prog" | sed -nE 's/.*avg ([0-9.]+) fps.*/\1/p')
    eta=$(print -r -- "$prog" | sed -nE 's/.*ETA ([0-9]+h[0-9]+m[0-9]+s).*/\1/p')
    print "Datei $i von $n"
    print "${key:t}\n"
    print "$(bar ${pct:-0})  ${pct:-0} %"
    print "Geschwindigkeit: ${fps:-–} fps   Restzeit diese Datei: ${eta:-–}\n"

    print "Noch offen:"
    cat "$BASE"/processing/*.txt(N) "$BASE"/queue/*.txt(N) 2>/dev/null | awk -v cur="$key" '
      $0==cur {seen=1; next}
      seen && NF { n=split($0,a,"/"); print "  • " a[n]; c++ }
      END { if (!c) print "  (keine weiteren)" }'
  else
    print "Kein Encoding aktiv."
    q=$(cat "$BASE"/queue/*.txt(N) 2>/dev/null | grep -c .)
    (( q > 0 )) && print "$q Datei(en) warten in der Warteschlange, Start folgt gleich."
  fi

  print "\nZuletzt abgeschlossen:"
  tail -n 5 "$LOGS/results.tsv" 2>/dev/null | awk -F'\t' '{n=split($3,a,"/"); print "  " $1 "  " $2 "  " a[n]}' \
    || print "  (noch nichts)"
  print "\n(Ctrl + C beendet nur diese Anzeige, nicht das Encoding)"
  sleep 3
done
