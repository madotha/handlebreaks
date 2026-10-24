#!/bin/zsh
# ─────────────────────────────────────────────────────────────────────────────
# status.sh – live view of the encoding in the terminal (like HandBrake's queue)
# Usage:  zsh ~/scripts/encoding/status.sh      Quit: Ctrl + C
# Quitting the view does NOT stop the encoding.
# ─────────────────────────────────────────────────────────────────────────────
BASE="${ENCODING_DATA_DIR:-${0:A:h}}"; LOGS="$BASE/logs"

bar() {  # progress bar, 40 characters
  local p=${1%.*}; [[ -z $p ]] && p=0
  local f=$(( p * 40 / 100 ))
  printf '[%s%s]' "${(l:$f::█:)}" "${(l:$((40-f))::░:)}"
}

while true; do
  clear
  print "═══════════════  HandBrake Encoding  ═══════════════"
  print "As of: $(date '+%H:%M:%S')\n"

  if [[ -f "$LOGS/current.txt" ]] && pgrep -x HandBrakeCLI >/dev/null; then
    IFS=$'\t' read -r key i n t < "$LOGS/current.txt"
    prog=$(tail -c 400 "$LOGS/progress.log" 2>/dev/null | tr '\r' '\n' | grep 'Encoding:' | tail -1)
    pct=$(print -r -- "$prog" | sed -nE 's/.*, ([0-9.]+) %.*/\1/p')
    fps=$(print -r -- "$prog" | sed -nE 's/.*avg ([0-9.]+) fps.*/\1/p')
    eta=$(print -r -- "$prog" | sed -nE 's/.*ETA ([0-9]+h[0-9]+m[0-9]+s).*/\1/p')
    print "File $i of $n"
    print "${key:t}\n"
    print "$(bar ${pct:-0})  ${pct:-0} %"
    print "Speed: ${fps:-–} fps   Time left for this file: ${eta:-–}\n"

    print "Still to do:"
    cat "$BASE"/processing/*.txt(N) "$BASE"/queue/*.txt(N) 2>/dev/null | awk -F'\t' -v cur="$key" '
      $1==cur {seen=1; next}
      seen && NF { n=split($1,a,"/"); print "  • " a[n]; c++ }
      END { if (!c) print "  (none)" }'
  else
    print "No encode running."
    q=$(cat "$BASE"/queue/*.txt(N) 2>/dev/null | grep -c .)
    (( q > 0 )) && print "$q file(s) waiting in the queue, starting shortly."
  fi

  print "\nRecently finished:"
  tail -n 5 "$LOGS/results.tsv" 2>/dev/null | awk -F'\t' '{n=split($3,a,"/"); print "  " $1 "  " $2 "  " a[n]}' \
    || print "  (nothing yet)"
  print "\n(Ctrl + C only closes this view, it does not stop the encoding)"
  sleep 3
done
