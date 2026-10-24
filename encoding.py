#!/usr/bin/env python3
"""
Encoding-Steuerung für die Plex-Bibliothek (Nachfolger von encoding_todo.py)
============================================================================

Was dieses Skript macht
-----------------------
1. scan     Durchsucht Filme und Serien auf dem NAS nach Dateien, die noch nicht
            in H.265 vorliegen, und schreibt die Obsidian-Notiz "Encoding To-Do.md".
            Offene Dateien bleiben auf der Liste, bis sie encodiert sind. Ein neuer
            Scan löscht also nichts mehr.
2. queue    Stellt ausgewählte Dateien (per Nummer aus dem letzten Scan) in die
            Warteschlange. Der Encoder (encode.sh, gestartet von launchd) arbeitet
            sie danach automatisch mit HandBrakeCLI und deinem Preset ab.
3. skip     Blendet Dateien dauerhaft aus. Sie erscheinen erst wieder, wenn sich
            der Dateiname ändert (z. B. bessere Version statt Line Dub).
4. status   Zeigt, wie weit das Encoding ist.

Was NICHT angeboten wird
------------------------
- Dateien mit x265 / H.265 / HEVC / FuN im Namen (bereits encodiert)
- Release-Typen wie LD, MD, TS, Telesync, CAM, HDCAM, TC, Line/Mic Dubbed
- Dateien, die schon fertig in _ENCODED liegen (warten auf deine Prüfung)
- Dateien, die bereits in der Warteschlange sind oder gerade encodiert werden
- Abgewählte Dateien und der Altbestand aus dem alten Cache

Pfade
-----
Alle Pfade stehen in config.env neben diesem Skript (Vorlage: config.example.env).
Pro Ordner sind mehrere Pfade mit ":" getrennt möglich, z. B. der echte macOS-Pfad
und ein Ersatzpfad in Claudes Arbeitsumgebung (~/media/...). Das Skript nimmt
jeweils den ersten Pfad, der existiert.

Beispiele
---------
    python3 encoding.py scan
    python3 encoding.py queue alle            # alles aus der letzten Liste
    python3 encoding.py queue alle -3 -7 --skip-rest
                                              # alles ausser 3 und 7, diese ausblenden
    python3 encoding.py queue 1,2,5-8
    python3 encoding.py skip 4
    python3 encoding.py skipped               # ausgeblendete Dateien anzeigen
    python3 encoding.py unskip 2              # Nr. 2 aus "skipped" wieder freigeben
    python3 encoding.py status
    python3 encoding.py version
"""

__version__ = "1.0.0"

import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

# ─── Konfiguration ────────────────────────────────────────────────────────────

BASE = Path(__file__).resolve().parent          # ~/scripts/encoding
CONFIG_FILE = BASE / "config.env"               # lokale Einstellungen, Vorlage: config.example.env

# Für jeden Ort eine Liste möglicher Pfade, gesetzt aus config.env (apply_config)
ROOT_CANDIDATES = {}
ENCODED_CANDIDATES = []
NOTE_DIR_CANDIDATES = []
NOTE_NAME = "Encoding To-Do.md"

STATE_FILE   = BASE / "state.json"
QUEUE_DIR    = BASE / "queue"         # neue Aufträge (launchd startet encode.sh)
PROC_DIR     = BASE / "processing"    # Auftrag in Arbeit
LOG_DIR      = BASE / "logs"
RESULTS_FILE = LOG_DIR / "results.tsv"
CURRENT_FILE = LOG_DIR / "current.txt"
PROGRESS_LOG = LOG_DIR / "progress.log"

# ─── Erkennung ────────────────────────────────────────────────────────────────

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".mpg", ".mpeg"}

# Bereits encodiert (wie im bisherigen Skript)
HEVC_RE = re.compile(r"x265|h\.?265|hevc|FuN", re.IGNORECASE)

# Schlechte Release-Typen. Sie müssen als eigener Namensteil vorkommen, also
# zwischen Punkten, Leerzeichen, Bindestrichen usw. Dadurch greift der Filter
# nicht bei Wörtern wie "Lord" oder "Old" und nicht bei der Endung ".ts"
# (geprüft wird nur der Dateiname ohne Endung).
SEP = r"[.\s_\-\[\]\(\)]"
BAD_RELEASE_RE = re.compile(
    rf"(?:^|{SEP})("
    r"LD|MD|TS|HDTS|TELESYNC|CAM|HDCAM|CAMRIP|TC|HDTC|TELECINE"
    r"|LINE[.\s_\-]?DUBBED|MIC[.\s_\-]?DUBBED|LINEDUB|MICDUB"
    rf")(?=$|{SEP})",
    re.IGNORECASE,
)


def load_config():
    """Liest config.env (Zeilen KEY="wert", wie sie auch zsh versteht)."""
    if not CONFIG_FILE.exists():
        raise SystemExit(f"FEHLER: {CONFIG_FILE} fehlt. Vorlage kopieren: "
                         f"cp config.example.env config.env, dann anpassen.")
    cfg = {}
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        parts = shlex.split(val, comments=True)
        cfg[key.strip()] = os.path.expandvars(parts[0]) if parts else ""
    return cfg


def path_list(value):
    """'a:b' -> ['a', 'b'], mit ~ aufgelöst."""
    return [os.path.expanduser(p) for p in value.split(":") if p]


def apply_config(cfg):
    global ROOT_CANDIDATES, ENCODED_CANDIDATES, NOTE_DIR_CANDIDATES
    missing = [k for k in ("FILME_DIR", "SERIEN_DIR", "ENCODED_DIR") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"FEHLER: In {CONFIG_FILE} fehlt: {', '.join(missing)}")
    ROOT_CANDIDATES = {"Filme": path_list(cfg["FILME_DIR"]), "Serien": path_list(cfg["SERIEN_DIR"])}
    ENCODED_CANDIDATES = path_list(cfg["ENCODED_DIR"])
    NOTE_DIR_CANDIDATES = path_list(cfg.get("NOTE_DIR", ""))


def first_existing(candidates):
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    return None


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ─── Zustand ──────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "baseline": [],     # Altbestand (alter Pfad ohne Filme/ bzw. Serien/)
        "skipped": {},      # Schlüssel -> Datum der Abwahl
        "known": [],        # schon einmal angebotene Dateien (für die Markierung "neu")
        "last_offer": [],   # Nummerierung der letzten Liste
        "last_scan": None,
    }


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def write_text_atomic(path, text):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ─── Warteschlange und Ergebnisse (gemeinsam mit encode.sh) ───────────────────

def queued_keys():
    """Alle Schlüssel, die in queue/ oder processing/ auf das Encoding warten."""
    keys = []
    for d in (PROC_DIR, QUEUE_DIR):
        if d.exists():
            for f in sorted(d.glob("*.txt")):
                keys += [l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return keys


def load_results():
    """Letztes Ergebnis pro Datei: OK, FAIL, MISSING oder EXISTS."""
    res = {}
    if RESULTS_FILE.exists():
        for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                res[parts[2]] = (parts[1], parts[0])
    return res


def encoded_stems(enc_dir):
    """Dateinamen (ohne Endung) der fertigen Dateien im _ENCODED-Ordner."""
    stems = set()
    if enc_dir and enc_dir.exists():
        for f in enc_dir.rglob("*"):
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(enc_dir).parts):
                stems.add(f.stem.lower())
    return stems


# ─── Scan ─────────────────────────────────────────────────────────────────────

def walk_library():
    """Liefert (schlüssel, alter_relativer_pfad, dateiname) für alle Videos."""
    missing = []
    for label, cands in ROOT_CANDIDATES.items():
        root = first_existing(cands)
        if root is None:
            missing.append(label)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):          # u. a. "._datei.mkv" vom NAS
                    continue
                if Path(fn).suffix.lower() not in VIDEO_EXTS:
                    continue
                full = Path(dirpath) / fn
                rel = str(full.relative_to(root))
                yield f"{label}/{rel}", rel, fn
    if missing:
        raise SystemExit(f"FEHLER: {', '.join(missing)} nicht erreichbar. Ist das NAS gemountet?")


def scan(state, quiet=False):
    enc_dir = first_existing(ENCODED_CANDIDATES)
    done_stems = encoded_stems(enc_dir)
    in_queue = set(queued_keys())
    results = load_results()
    baseline = set(state["baseline"])
    skipped = state["skipped"]
    known = set(state["known"])

    offer, waiting, queued, bad, hid_skip, hid_base = [], [], [], [], [], []
    n_total = n_encoded = 0
    all_unencoded = set()

    for key, old_rel, fn in walk_library():
        n_total += 1
        if HEVC_RE.search(fn):
            n_encoded += 1
            continue
        all_unencoded.add(key)
        stem = Path(fn).stem
        if BAD_RELEASE_RE.search(stem):
            bad.append(key)
        elif stem.lower() in done_stems:
            waiting.append(key)
        elif key in in_queue:
            queued.append(key)
        elif key in skipped:
            hid_skip.append(key)
        elif old_rel in baseline:
            hid_base.append(key)
        else:
            offer.append(key)

    offer.sort(key=str.lower)
    flags = {}
    for k in offer:
        r = results.get(k)
        if r and r[0] == "FAIL":
            flags[k] = "⚠️ letztes Encoding fehlgeschlagen"
        elif k not in known:
            flags[k] = "🆕"

    # Abgewählte Dateien, die es nicht mehr gibt, aus dem Zustand entfernen
    state["skipped"] = {k: v for k, v in skipped.items() if k in all_unencoded}
    state["known"] = sorted(known | set(offer))
    state["last_offer"] = offer
    state["last_scan"] = now_str()
    save_state(state)

    summary = {
        "scan": state["last_scan"],
        "videos_total": n_total,
        "bereits_h265": n_encoded,
        "zur_auswahl": [{"nr": i + 1, "datei": k, "hinweis": flags.get(k, "")} for i, k in enumerate(offer)],
        "in_warteschlange": sorted(queued, key=str.lower),
        "encodiert_wartet_auf_pruefung": sorted(waiting, key=str.lower),
        "ausgefiltert_release_typ": sorted(bad, key=str.lower),
        "ausgeblendet_abgewaehlt": len(hid_skip),
        "ausgeblendet_altbestand": len(hid_base),
        "encoded_ordner": str(enc_dir) if enc_dir else None,
    }
    write_note(summary)
    return summary


# ─── Obsidian-Notiz ───────────────────────────────────────────────────────────

def short(key):
    """Kurze, gut lesbare Darstellung: Ordner der Serie/des Films + Dateiname."""
    parts = key.split("/")
    return f"{parts[1]} › {parts[-1]}" if len(parts) > 2 else key


def write_note(s):
    note_dir = first_existing(NOTE_DIR_CANDIDATES)
    if note_dir is None:
        print("WARNUNG: Obsidian-Ordner nicht gefunden, Notiz nicht geschrieben.", file=sys.stderr)
        return
    L = [
        "# Encoding To-Do",
        f"> Aktualisiert: {s['scan']} | {s['videos_total']} Videos, davon {s['bereits_h265']} bereits H.265",
        "",
        "## ⏳ Encoding-Status",
        f"*Stand beim Scan. Live-Status: `zsh status.sh` im Terminal oder Claude fragen.*",
        "",
        status_text(),
        "",
        f"## 🔧 Zur Auswahl ({len(s['zur_auswahl'])})",
    ]
    if s["zur_auswahl"]:
        for item in s["zur_auswahl"]:
            hint = f" {item['hinweis']}" if item["hinweis"] else ""
            L.append(f"- [ ] **{item['nr']}.** {short(item['datei'])}{hint}")
    else:
        L.append("*Nichts Neues zu encodieren. ✓*")
    L += ["", f"## 📥 In Warteschlange ({len(s['in_warteschlange'])})"]
    L += [f"- {short(k)}" for k in s["in_warteschlange"]] or ["*leer*"]
    L += ["", f"## 🔍 Encodiert, wartet auf deine Prüfung in _ENCODED ({len(s['encodiert_wartet_auf_pruefung'])})"]
    L += [f"- {short(k)}" for k in s["encodiert_wartet_auf_pruefung"]] or ["*nichts*"]
    L += ["", f"## 🚫 Ausgefiltert wegen Release-Typ (LD, TS, CAM …) ({len(s['ausgefiltert_release_typ'])})"]
    L += [f"- {short(k)}" for k in s["ausgefiltert_release_typ"]] or ["*keine*"]
    L += [
        "",
        "---",
        f"Ausgeblendet: {s['ausgeblendet_abgewaehlt']} abgewählt, {s['ausgeblendet_altbestand']} Altbestand",
        "",
    ]
    write_text_atomic(note_dir / NOTE_NAME, "\n".join(L))


# ─── Status ───────────────────────────────────────────────────────────────────

def status_text():
    q = queued_keys()
    lines = []
    if CURRENT_FILE.exists():
        parts = CURRENT_FILE.read_text(encoding="utf-8").strip().split("\t")
        if len(parts) >= 3:
            key, i, n = parts[0], parts[1], parts[2]
            prog = ""
            if PROGRESS_LOG.exists():
                raw = PROGRESS_LOG.read_bytes()[-2000:].decode("utf-8", "replace")
                chunks = [c for c in re.split(r"[\r\n]", raw) if "Encoding:" in c]
                if chunks:
                    last = chunks[-1]
                    pct = re.search(r"([\d.]+) %", last)
                    eta = re.search(r"ETA (\d+h\d+m\d+s)", last)
                    if pct:
                        prog = f", {pct.group(1)} %" + (f", Restzeit {eta.group(1)}" if eta else "")
            lines.append(f"**Läuft:** Datei {i} von {n}: {short(key)}{prog}")
    if not lines:
        lines.append("Kein Encoding aktiv.")
    if q:
        lines.append(f"Insgesamt {len(q)} Datei(en) in Auftrag (inkl. laufender).")
    res = load_results()
    recent = sorted(res.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    if recent:
        lines.append("Zuletzt: " + "; ".join(f"{v[0]} {short(k)} ({v[1]})" for k, v in recent))
    return "\n".join(lines)


# ─── Auswahl ──────────────────────────────────────────────────────────────────

def parse_selection(tokens, n):
    """'alle', '1,2', '5-8', '-3' (ausschliessen). Liefert sortierte 1-basierte Nummern."""
    include, exclude, all_ = set(), set(), False
    for tok in re.split(r"[,\s]+", " ".join(tokens).strip()):
        if not tok:
            continue
        neg = tok.startswith(("-", "!")) and tok[1:2].isdigit()
        t = tok[1:] if neg else tok
        if t.lower() in ("alle", "all", "a"):
            all_ = True
            continue
        if re.fullmatch(r"\d+-\d+", t):
            a, b = map(int, t.split("-"))
            nums = set(range(min(a, b), max(a, b) + 1))
        elif t.isdigit():
            nums = {int(t)}
        else:
            raise SystemExit(f"Unbekannte Auswahl: {tok}")
        (exclude if neg else include).update(nums)
    sel = (set(range(1, n + 1)) if all_ else include) - exclude
    bad = [x for x in sel if x < 1 or x > n]
    if bad:
        raise SystemExit(f"Nummer(n) ausserhalb der Liste (1-{n}): {bad}")
    return sorted(sel)


def cmd_queue(state, args):
    skip_rest = "--skip-rest" in args
    args = [a for a in args if a != "--skip-rest"]
    offer = state["last_offer"]
    if not offer:
        raise SystemExit("Die letzte Liste ist leer. Zuerst 'scan' ausführen.")
    nums = parse_selection(args, len(offer))
    already = set(queued_keys())
    chosen = [offer[i - 1] for i in nums if offer[i - 1] not in already]
    if chosen:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        batch = QUEUE_DIR / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        tmp = BASE / f".{batch.name}.tmp"
        tmp.write_text("\n".join(chosen) + "\n", encoding="utf-8")
        os.replace(tmp, batch)   # erst jetzt sieht launchd den Auftrag
    skipped_now = []
    if skip_rest:
        for i, k in enumerate(offer, 1):
            if i not in nums:
                state["skipped"][k] = now_str()
                skipped_now.append(k)
    save_state(state)   # Nummerierung bleibt bis zum nächsten Scan gleich
    print(f"{len(chosen)} Datei(en) in die Warteschlange gestellt:")
    for k in chosen:
        print(f"  + {short(k)}")
    for k in skipped_now:
        print(f"  – ausgeblendet: {short(k)}")


def cmd_skip(state, args):
    offer = state["last_offer"]
    nums = parse_selection(args, len(offer))
    for i in nums:
        state["skipped"][offer[i - 1]] = now_str()
        print(f"  – ausgeblendet: {short(offer[i - 1])}")
    save_state(state)


def cmd_skipped(state):
    items = sorted(state["skipped"].items(), key=lambda kv: kv[0].lower())
    if not items:
        print("Keine abgewählten Dateien.")
    for i, (k, d) in enumerate(items, 1):
        print(f"{i:3}. {short(k)}  (abgewählt {d})")


def cmd_unskip(state, args):
    items = sorted(state["skipped"], key=str.lower)
    for i in parse_selection(args, len(items)):
        state["skipped"].pop(items[i - 1], None)
        print(f"  ↺ wieder freigegeben: {short(items[i - 1])}")
    save_state(state)


# ─── Übernahme der Historie aus dem alten Skript ──────────────────────────────

def cmd_import_legacy(state, cache_path, note_path):
    """Altbestand = alles, was im alten Cache als 'noch zu encoden' stand,
    ausser den Dateien, die in der letzten Notiz unter 'Noch zu encoden' standen.
    Diese bleiben sichtbar, ebenso alles, was seither neu dazugekommen ist."""
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    last_list = set()
    if note_path and Path(note_path).exists():
        section = False
        for line in Path(note_path).read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section = "Noch zu encoden" in line
            elif section and line.startswith("- ["):
                last_list.add(line[6:].strip())
    baseline = set(cache.get("seen_todo", [])) - last_list
    state["baseline"] = sorted(baseline)
    save_state(state)
    print(f"Altbestand übernommen: {len(baseline)} Dateien ausgeblendet, "
          f"{len(last_list)} aus der letzten Liste bleiben sichtbar "
          f"(alter Scan vom {cache.get('last_scan')}).")


# ─── Hauptprogramm ────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd in ("version", "--version", "-V"):
        print(f"encoding.py v{__version__}")
        return
    apply_config(load_config())
    for d in (QUEUE_DIR, PROC_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    state = load_state()

    if cmd == "scan":
        s = scan(state)
        if "--json" in rest:
            print(json.dumps(s, indent=1, ensure_ascii=False))
            return
        print(f"Scan {s['scan']}: {s['videos_total']} Videos, {s['bereits_h265']} bereits H.265\n")
        print(f"Zur Auswahl ({len(s['zur_auswahl'])}):")
        for it in s["zur_auswahl"]:
            print(f"  {it['nr']:3}. {short(it['datei'])} {it['hinweis']}")
        print(f"\nIn Warteschlange: {len(s['in_warteschlange'])}")
        print(f"Encodiert, wartet auf Prüfung: {len(s['encodiert_wartet_auf_pruefung'])}")
        print(f"Ausgefiltert (LD/TS/…): {len(s['ausgefiltert_release_typ'])}")
        print(f"Ausgeblendet: {s['ausgeblendet_abgewaehlt']} abgewählt, {s['ausgeblendet_altbestand']} Altbestand")
    elif cmd == "queue":
        cmd_queue(state, rest)
    elif cmd == "skip":
        cmd_skip(state, rest)
    elif cmd == "skipped":
        cmd_skipped(state)
    elif cmd == "unskip":
        cmd_unskip(state, rest)
    elif cmd == "status":
        print(status_text())
    elif cmd == "import-legacy":
        cmd_import_legacy(state, rest[0], rest[1] if len(rest) > 1 else None)
    else:
        print(__doc__)
        raise SystemExit(f"Unbekannter Befehl: {cmd}")


if __name__ == "__main__":
    main()
