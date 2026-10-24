#!/usr/bin/env python3
"""
Encoding control for a Plex library
===================================

What this script does
---------------------
1. scan     Searches the movie and TV show folders for files that aren't H.265 yet
            and writes the Obsidian note "Encoding To-Do.md". Open files stay on the
            list until they are encoded, so a new scan never drops anything. ffprobe
            also reads codec, resolution, duration and bitrate (only for new or
            changed files; turn it off with --no-probe).
2. queue    Puts selected files (by number from the last scan) into the queue. The
            worker (encode.sh, started by launchd or the Docker container) then
            encodes them with HandBrakeCLI and your preset.
3. skip     Hides files permanently. They only reappear if the file name changes
            (e.g. a better release replacing a line dub).
4. status   Shows how far encoding has progressed.

What is NOT offered
-------------------
- Files with x265 / H.265 / HEVC / FuN in the name, or whose video track is already
  in an optimized codec according to ffprobe (HEVC, AV1, VVC)
- Release types such as LD, MD, TS, Telesync, CAM, HDCAM, TC, Line/Mic Dubbed
- Files already finished in the output folder (waiting for your review)
- Files already in the queue or being encoded
- Skipped files and the legacy baseline from the old cache

Paths and data
--------------
All paths are set in config.env in the data folder (template: config.example.env).
Each folder setting may list several paths separated by ":", e.g. the real macOS
path and a fallback path inside the Docker container or another environment
(~/media/...). The script uses the first path that exists.

Each section can have several library folders: MOVIES_DIR, MOVIES_DIR_2, MOVIES_DIR_3, …
and SHOWS_DIR, SHOWS_DIR_2, … Their files are keyed as "Movies/…", "Movies 2/…" etc.,
so keep each folder's number when editing config.env.

The data folder is the folder of this script, unless the environment variable
ENCODING_DATA_DIR points elsewhere (e.g. /data in the Docker container). It holds
config.env, the database encoding.db (SQLite), queue/, processing/, done/ and logs/.
A state.json from version 1.x is imported into the database on first run.

Examples
--------
    python3 encoding.py scan
    python3 encoding.py scan --no-probe       # without ffprobe (faster)
    python3 encoding.py queue all             # everything from the last list
    python3 encoding.py queue all -3 -7 --skip-rest
                                              # everything except 3 and 7, and hide those
    python3 encoding.py queue 1,2,5-8
    python3 encoding.py skip 4
    python3 encoding.py skipped               # list hidden files
    python3 encoding.py unskip 2              # show no. 2 from "skipped" again
    python3 encoding.py status
    python3 encoding.py version
"""

__version__ = "2.0.0"

import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────

BASE = Path(__file__).resolve().parent          # program folder, e.g. ~/scripts/encoding
DATA_DIR = Path(os.environ.get("ENCODING_DATA_DIR") or BASE)
CONFIG_FILE = DATA_DIR / "config.env"           # local settings, template: config.example.env
EXAMPLE_CONFIG = BASE / "config.example.env"
DB_FILE = Path(os.environ.get("ENCODING_DB_FILE") or DATA_DIR / "encoding.db")

# For each location a list of possible paths, set from config.env (apply_config)
ROOT_CANDIDATES = {}
ENCODED_CANDIDATES = []
NOTE_DIR_CANDIDATES = []
NOTE_NAME = "Encoding To-Do.md"

# Library sections: section -> config.env setting. Each section can have several
# library folders: MOVIES_DIR, MOVIES_DIR_2, MOVIES_DIR_3, … Every folder gets its
# own key prefix ("Movies", "Movies 2", …), so the number must not change later.
SECTIONS = {"Movies": "MOVIES_DIR", "Shows": "SHOWS_DIR"}
# Version 1.x used German folder labels as key prefixes. Only used to import old
# data (state.json, results.tsv), so these words must stay as they are.
LEGACY_PREFIXES = {"Filme/": "Movies/", "Serien/": "Shows/"}
LEGACY_NOTE_HEADING = "Noch zu encoden"     # section heading in the predecessor's note (import-legacy)

STATE_FILE   = DATA_DIR / "state.json"    # only used to import data from version 1.x
SCAN_LOCK    = DATA_DIR / ".scan.lock"
QUEUE_DIR    = DATA_DIR / "queue"         # new batches (picked up by encode.sh)
PROC_DIR     = DATA_DIR / "processing"    # batch being encoded
LOG_DIR      = DATA_DIR / "logs"
RESULTS_FILE = LOG_DIR / "results.tsv"
CURRENT_FILE = LOG_DIR / "current.txt"
PROGRESS_LOG = LOG_DIR / "progress.log"

# Find ffprobe even when launchd or another caller sets a minimal PATH
FFPROBE = shutil.which("ffprobe", path=os.pathsep.join(
    [os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]))
PROBE_WORKERS = 4                        # parallel ffprobe calls (network share)

# Status of a file after the last scan (column files.status)
STATUS_LABELS = {
    "offer":    "Offered",
    "queued":   "Queued",
    "waiting":  "Encoded, awaiting review",
    "h265":     "Already optimized",
    "bad":      "Filtered (release type)",
    "skipped":  "Skipped",
    "baseline": "Legacy baseline",
}

# ─── Detection ────────────────────────────────────────────────────────────────

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".mpg", ".mpeg"}

# Codecs that count as optimized: such files are never offered for encoding
OPTIMIZED_CODECS = {"hevc", "av1", "vvc"}

# Already encoded (judged by the file name)
HEVC_RE = re.compile(r"x265|h\.?265|hevc|FuN", re.IGNORECASE)

# Poor release types. They must appear as a separate part of the name, i.e.
# between dots, spaces, hyphens etc. That way the filter doesn't match words like
# "Lord" or "Old", nor the ".ts" extension (only the name without extension is checked).
SEP = r"[.\s_\-\[\]\(\)]"
BAD_RELEASE_RE = re.compile(
    rf"(?:^|{SEP})("
    r"LD|MD|TS|HDTS|TELESYNC|CAM|HDCAM|CAMRIP|TC|HDTC|TELECINE"
    r"|LINE[.\s_\-]?DUBBED|MIC[.\s_\-]?DUBBED|LINEDUB|MICDUB"
    rf")(?=$|{SEP})",
    re.IGNORECASE,
)


def parse_config(path, expand=True):
    """Read KEY="value" lines the way zsh reads them. expand=False keeps $HOME as written."""
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        parts = shlex.split(val, comments=True)
        val = parts[0] if parts else ""
        cfg[key.strip()] = os.path.expandvars(val) if expand else val
    return cfg


def load_config():
    """Read config.env."""
    if not CONFIG_FILE.exists():
        raise SystemExit(f"ERROR: {CONFIG_FILE} is missing. Copy the template: "
                         f"cp config.example.env config.env, then adjust it.")
    return parse_config(CONFIG_FILE)


def path_list(value):
    """'a:b' -> ['a', 'b'], with ~ expanded."""
    return [os.path.expanduser(p) for p in value.split(":") if p]


def library_roots(cfg):
    """Library folders from config.env: [(prefix, section, setting, value)], in a stable order.
    MOVIES_DIR -> "Movies", MOVIES_DIR_2 -> "Movies 2", …; empty settings are ignored."""
    roots = []
    for section, base in SECTIONS.items():
        numbered = sorted((int(m.group(1)), k) for k in cfg
                          if (m := re.fullmatch(rf"{base}_(\d+)", k)) and int(m.group(1)) >= 2)
        for n, key in [(1, base)] + numbered:
            if cfg.get(key):
                roots.append((section if n == 1 else f"{section} {n}", section, key, cfg[key]))
    return roots


def section_of(key):
    """Section ("Movies" or "Shows") of a file key such as "Movies 2/Film/Film.mkv"."""
    return key.split("/", 1)[0].split(" ", 1)[0]


def apply_config(cfg):
    global ROOT_CANDIDATES, ENCODED_CANDIDATES, NOTE_DIR_CANDIDATES
    missing = [k for k in (*SECTIONS.values(), "ENCODED_DIR") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"ERROR: {CONFIG_FILE} is missing: {', '.join(missing)}")
    ROOT_CANDIDATES = {prefix: path_list(value) for prefix, _, _, value in library_roots(cfg)}
    ENCODED_CANDIDATES = path_list(cfg["ENCODED_DIR"])
    NOTE_DIR_CANDIDATES = path_list(cfg.get("NOTE_DIR", ""))


def first_existing(candidates):
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    return None


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ─── Database (SQLite) ────────────────────────────────────────────────────────
# The database must be on a local disk, not on the NAS.

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta     (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS baseline (rel TEXT PRIMARY KEY);     -- legacy baseline (path without the section prefix)
CREATE TABLE IF NOT EXISTS skipped  (key TEXT PRIMARY KEY, since TEXT);
CREATE TABLE IF NOT EXISTS known    (key TEXT PRIMARY KEY);     -- offered before (for the "new" marker)
CREATE TABLE IF NOT EXISTS results  (at TEXT, status TEXT, key TEXT, PRIMARY KEY (at, status, key));
CREATE TABLE IF NOT EXISTS files (                              -- inventory from the last scan
    key TEXT PRIMARY KEY, size INTEGER, mtime REAL,
    video_codec TEXT, width INTEGER, height INTEGER, duration REAL, bitrate INTEGER,
    probed_at TEXT, probe_error TEXT, status TEXT
);
"""


def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    migrate_state_json(con)
    return con


def migrate_state_json(con):
    """Import state.json from version 1.x into the database, once."""
    if not STATE_FILE.exists():
        return
    old = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    with con:
        con.executemany("INSERT OR IGNORE INTO baseline VALUES (?)", [(r,) for r in old.get("baseline", [])])
        con.executemany("INSERT OR IGNORE INTO skipped VALUES (?, ?)",
                        [(legacy_key(k), v) for k, v in old.get("skipped", {}).items()])
        con.executemany("INSERT OR IGNORE INTO known VALUES (?)", [(legacy_key(k),) for k in old.get("known", [])])
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("last_offer", json.dumps([legacy_key(k) for k in old.get("last_offer", [])], ensure_ascii=False)),
            ("last_scan", old.get("last_scan")),
        ])
    try:
        STATE_FILE.rename(STATE_FILE.with_name("state.json.migrated"))
        print("Imported state.json into the database (old file: state.json.migrated).", file=sys.stderr)
    except FileNotFoundError:
        pass                                    # imported by another process at the same time


def legacy_key(key):
    """Translate a key from version 1.x (German section prefix) to the current prefix."""
    for old, new in LEGACY_PREFIXES.items():
        if key.startswith(old):
            return new + key[len(old):]
    return key


def load_state():
    with closing(connect()) as con:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        return {
            "baseline": [r for (r,) in con.execute("SELECT rel FROM baseline")],
            "skipped": dict(con.execute("SELECT key, since FROM skipped")),
            "known": [k for (k,) in con.execute("SELECT key FROM known")],
            "last_offer": json.loads(meta.get("last_offer") or "[]"),   # numbering of the last list
            "last_scan": meta.get("last_scan"),
        }


def save_state(state):
    with closing(connect()) as con, con:
        con.execute("DELETE FROM baseline")
        con.executemany("INSERT INTO baseline VALUES (?)", [(r,) for r in state["baseline"]])
        con.execute("DELETE FROM skipped")
        con.executemany("INSERT INTO skipped VALUES (?, ?)", state["skipped"].items())
        con.execute("DELETE FROM known")
        con.executemany("INSERT INTO known VALUES (?)", [(k,) for k in state["known"]])
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("last_offer", json.dumps(state["last_offer"], ensure_ascii=False)),
            ("last_scan", state["last_scan"]),
        ])


FILE_COLUMNS = ("key", "size", "mtime", "video_codec", "width", "height", "duration",
                "bitrate", "probed_at", "probe_error", "status")


def load_files():
    """Inventory from the last scan: key -> dict with FILE_COLUMNS."""
    with closing(connect()) as con:
        rows = con.execute(f"SELECT {', '.join(FILE_COLUMNS)} FROM files")
        return {r[0]: dict(zip(FILE_COLUMNS, r)) for r in rows}


def save_files(infos):
    with closing(connect()) as con, con:
        con.execute("DELETE FROM files")
        con.executemany(
            f"INSERT INTO files ({', '.join(FILE_COLUMNS)}) VALUES ({', '.join('?' * len(FILE_COLUMNS))})",
            [tuple(i.get(c) for c in FILE_COLUMNS) for i in infos.values()],
        )


@contextmanager
def scan_lock():
    """Prevents two scans at the same time (e.g. web UI and terminal)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCAN_LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A scan is already running.")
        yield


def write_text_atomic(path, text):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ─── Queue and results (shared with encode.sh) ────────────────────────────────

def queued_keys():
    """All keys waiting in queue/ or processing/ to be encoded."""
    keys = []
    for d in (PROC_DIR, QUEUE_DIR):
        if d.exists():
            for f in sorted(d.glob("*.txt")):
                keys += [l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return keys


def load_results():
    """Latest result per file: OK, FAIL, MISSING or EXISTS.
    New lines from logs/results.tsv (written by encode.sh) are imported into the database first."""
    with closing(connect()) as con:
        if RESULTS_FILE.exists():
            rows = [(p[0], p[1], legacy_key(p[2])) for p in (l.split("\t") for l in
                    RESULTS_FILE.read_text(encoding="utf-8").splitlines()) if len(p) >= 3]
            with con:
                con.executemany("INSERT OR IGNORE INTO results (at, status, key) VALUES (?, ?, ?)", rows)
        res = {}
        for at, status, key in con.execute("SELECT at, status, key FROM results ORDER BY at, rowid"):
            res[key] = (status, at)
        return res


def recent_results(n=20):
    """The last n results as (time, status, key), newest first."""
    load_results()
    with closing(connect()) as con:
        return con.execute("SELECT at, status, key FROM results ORDER BY at DESC, rowid DESC LIMIT ?",
                           (n,)).fetchall()


def encoded_stems(enc_dir):
    """File names (without extension) of the finished files in the output folder."""
    stems = set()
    if enc_dir and enc_dir.exists():
        for f in enc_dir.rglob("*"):
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(enc_dir).parts):
                stems.add(f.stem.lower())
    return stems


# ─── Scan ─────────────────────────────────────────────────────────────────────

def walk_library():
    """Yields (key, relative_path, file_name, full_path) for every video."""
    missing = []
    for label, cands in ROOT_CANDIDATES.items():
        root = first_existing(cands)
        if root is None:
            missing.append(label)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):          # e.g. "._file.mkv" from the NAS
                    continue
                if Path(fn).suffix.lower() not in VIDEO_EXTS:
                    continue
                full = Path(dirpath) / fn
                rel = str(full.relative_to(root))
                yield f"{label}/{rel}", rel, fn, full
    if missing:
        raise SystemExit(f"ERROR: {', '.join(missing)} not reachable. Is the NAS mounted?")


def to_num(value, typ):
    try:
        return typ(value)
    except (TypeError, ValueError):
        return None


def probe(path):
    """Read codec, resolution, duration and bitrate of the first video track with ffprobe."""
    info = {"probed_at": now_str(), "video_codec": None, "width": None, "height": None,
            "duration": None, "bitrate": None, "probe_error": None}
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height:format=duration,bit_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        info["probe_error"] = str(e)
        return info
    if out.returncode != 0:
        info["probe_error"] = out.stderr.strip()[:500] or f"ffprobe exit code {out.returncode}"
        return info
    data = json.loads(out.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    info.update(video_codec=stream.get("codec_name"), width=stream.get("width"),
                height=stream.get("height"), duration=to_num(fmt.get("duration"), float),
                bitrate=to_num(fmt.get("bit_rate"), int))
    return info


def inventory(entries, probe_files):
    """Size, modification time and (if wanted) ffprobe data per file.
    ffprobe only runs for new or changed files; the rest comes from the database."""
    cached = load_files()
    infos, todo = {}, []
    for key, _, _, full in entries:
        try:
            st = full.stat()
        except OSError:
            continue
        info = {"key": key, "size": st.st_size, "mtime": st.st_mtime}
        old = cached.get(key)
        if old and old["size"] == st.st_size and old["mtime"] == st.st_mtime and old["probed_at"]:
            info.update({c: old[c] for c in ("video_codec", "width", "height", "duration",
                                              "bitrate", "probed_at", "probe_error")})
        elif probe_files and FFPROBE:
            todo.append((key, full))
        infos[key] = info

    if todo:
        print(f"ffprobe: {len(todo)} new or changed file(s) …", file=sys.stderr)
        with ThreadPoolExecutor(PROBE_WORKERS) as pool:
            for n, (key, data) in enumerate(zip([k for k, _ in todo],
                                                pool.map(probe, [f for _, f in todo])), 1):
                infos[key].update(data)
                if n % 50 == 0:
                    print(f"ffprobe: {n}/{len(todo)}", file=sys.stderr)
    return infos


def scan(state, probe_files=True):
    with scan_lock():
        return _scan(state, probe_files)


def _scan(state, probe_files):
    enc_dir = first_existing(ENCODED_CANDIDATES)
    done_stems = encoded_stems(enc_dir)
    in_queue = set(queued_keys())
    results = load_results()
    baseline = set(state["baseline"])
    skipped = state["skipped"]
    known = set(state["known"])

    entries = list(walk_library())
    infos = inventory(entries, probe_files)

    offer, waiting, queued, bad, hid_skip, hid_base = [], [], [], [], [], []
    n_total = n_encoded = 0
    all_unencoded = set()

    for key, old_rel, fn, _ in entries:
        if key not in infos:                    # disappeared during the scan
            continue
        info = infos[key]
        n_total += 1
        if HEVC_RE.search(fn) or info.get("video_codec") in OPTIMIZED_CODECS:
            n_encoded += 1
            info["status"] = "h265"
            continue
        all_unencoded.add(key)
        stem = Path(fn).stem
        if BAD_RELEASE_RE.search(stem):
            bad.append(key)
            info["status"] = "bad"
        elif stem.lower() in done_stems:
            waiting.append(key)
            info["status"] = "waiting"
        elif key in in_queue:
            queued.append(key)
            info["status"] = "queued"
        elif key in skipped:
            hid_skip.append(key)
            info["status"] = "skipped"
        elif old_rel in baseline:
            hid_base.append(key)
            info["status"] = "baseline"
        else:
            offer.append(key)
            info["status"] = "offer"
    save_files(infos)

    offer.sort(key=str.lower)
    flags = {}
    for k in offer:
        r = results.get(k)
        if r and r[0] == "FAIL":
            flags[k] = "⚠️ last encode failed"
        elif k not in known:
            flags[k] = "🆕"

    # Forget skipped files that no longer exist
    state["skipped"] = {k: v for k, v in skipped.items() if k in all_unencoded}
    state["known"] = sorted(known | set(offer))
    state["last_offer"] = offer
    state["last_scan"] = now_str()
    save_state(state)

    summary = {
        "scan": state["last_scan"],
        "videos_total": n_total,
        "already_h265": n_encoded,
        "offer": [{"nr": i + 1, "file": k, "hint": flags.get(k, "")} for i, k in enumerate(offer)],
        "queued": sorted(queued, key=str.lower),
        "awaiting_review": sorted(waiting, key=str.lower),
        "filtered_release_type": sorted(bad, key=str.lower),
        "hidden_skipped": len(hid_skip),
        "hidden_baseline": len(hid_base),
        "encoded_dir": str(enc_dir) if enc_dir else None,
        "ffprobe": bool(FFPROBE) and probe_files,
    }
    write_note(summary)
    return summary


# ─── Obsidian note ────────────────────────────────────────────────────────────

def short(key):
    """Short, readable form: folder of the show/movie + file name. Files from an
    additional library folder ("Movies 2", …) are prefixed with its name."""
    parts = key.split("/")
    tag = f"{parts[0]}: " if " " in parts[0] else ""
    return f"{tag}{parts[1]} › {parts[-1]}" if len(parts) > 2 else key


def write_note(s):
    if not NOTE_DIR_CANDIDATES:                 # NOTE_DIR empty: no notes wanted
        return
    note_dir = first_existing(NOTE_DIR_CANDIDATES)
    if note_dir is None:
        print("WARNING: Obsidian folder not found, note not written.", file=sys.stderr)
        return
    L = [
        "# Encoding To-Do",
        f"> Updated: {s['scan']} | {s['videos_total']} videos, {s['already_h265']} of them already H.265",
        "",
        "## ⏳ Encoding status",
        f"*As of the scan. For live status, use the web UI or `status.sh`.*",
        "",
        status_text(),
        "",
        f"## 🔧 Offered ({len(s['offer'])})",
    ]
    if s["offer"]:
        for item in s["offer"]:
            hint = f" {item['hint']}" if item["hint"] else ""
            L.append(f"- [ ] **{item['nr']}.** {short(item['file'])}{hint}")
    else:
        L.append("*Nothing new to encode. ✓*")
    L += ["", f"## 📥 Queued ({len(s['queued'])})"]
    L += [f"- {short(k)}" for k in s["queued"]] or ["*empty*"]
    L += ["", f"## 🔍 Encoded, awaiting your review in the output folder ({len(s['awaiting_review'])})"]
    L += [f"- {short(k)}" for k in s["awaiting_review"]] or ["*none*"]
    L += ["", f"## 🚫 Filtered by release type (LD, TS, CAM …) ({len(s['filtered_release_type'])})"]
    L += [f"- {short(k)}" for k in s["filtered_release_type"]] or ["*none*"]
    L += [
        "",
        "---",
        f"Hidden: {s['hidden_skipped']} skipped, {s['hidden_baseline']} legacy baseline",
        "",
    ]
    write_text_atomic(note_dir / NOTE_NAME, "\n".join(L))


# ─── Status ───────────────────────────────────────────────────────────────────

def current_progress():
    """The file being encoded, with progress from progress.log, or None."""
    if not CURRENT_FILE.exists():
        return None
    parts = CURRENT_FILE.read_text(encoding="utf-8").strip().split("\t")
    if len(parts) < 3:
        return None
    cur = {"key": parts[0], "i": parts[1], "n": parts[2], "pct": None, "eta": None, "fps": None}
    if PROGRESS_LOG.exists():
        raw = PROGRESS_LOG.read_bytes()[-2000:].decode("utf-8", "replace")
        chunks = [c for c in re.split(r"[\r\n]", raw) if "Encoding:" in c]
        if chunks:
            for name, rx in (("pct", r"([\d.]+) %"), ("eta", r"ETA (\d+h\d+m\d+s)"),
                             ("fps", r"avg ([\d.]+) fps")):
                m = re.search(rx, chunks[-1])
                cur[name] = m.group(1) if m else None
    return cur


def status_text():
    q = queued_keys()
    lines = []
    cur = current_progress()
    if cur:
        prog = ""
        if cur["pct"]:
            prog = f", {cur['pct']} %" + (f", {cur['eta']} left" if cur["eta"] else "")
        lines.append(f"**Running:** file {cur['i']} of {cur['n']}: {short(cur['key'])}{prog}")
    if not lines:
        lines.append("No encode running.")
    if q:
        lines.append(f"{len(q)} file(s) queued in total (including the current one).")
    res = load_results()
    recent = sorted(res.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    if recent:
        lines.append("Recent: " + "; ".join(f"{v[0]} {short(k)} ({v[1]})" for k, v in recent))
    return "\n".join(lines)


# ─── Selection ────────────────────────────────────────────────────────────────

def parse_selection(tokens, n):
    """'all', '1,2', '5-8', '-3' (exclude). Returns sorted 1-based numbers."""
    include, exclude, all_ = set(), set(), False
    for tok in re.split(r"[,\s]+", " ".join(tokens).strip()):
        if not tok:
            continue
        neg = tok.startswith(("-", "!")) and tok[1:2].isdigit()
        t = tok[1:] if neg else tok
        if t.lower() in ("all", "a"):
            all_ = True
            continue
        if re.fullmatch(r"\d+-\d+", t):
            a, b = map(int, t.split("-"))
            nums = set(range(min(a, b), max(a, b) + 1))
        elif t.isdigit():
            nums = {int(t)}
        else:
            raise SystemExit(f"Unknown selection: {tok}")
        (exclude if neg else include).update(nums)
    sel = (set(range(1, n + 1)) if all_ else include) - exclude
    bad = [x for x in sel if x < 1 or x > n]
    if bad:
        raise SystemExit(f"Number(s) outside the list (1-{n}): {bad}")
    return sorted(sel)


def write_batch(keys):
    """Queue the given keys as a new batch. Keys already queued are left out.
    Returns the keys that were actually queued."""
    already = set(queued_keys())
    chosen = [k for k in dict.fromkeys(keys) if k not in already]
    if not chosen:
        return []
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    batch = QUEUE_DIR / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.txt"
    tmp = DATA_DIR / f".{batch.name}.tmp"         # same disk as queue/, so os.replace is atomic
    tmp.write_text("\n".join(chosen) + "\n", encoding="utf-8")
    os.replace(tmp, batch)                       # only now does the worker see the batch
    with closing(connect()) as con, con:         # show them as queued before the next scan
        con.executemany("UPDATE files SET status = 'queued' WHERE key = ?", [(k,) for k in chosen])
    return chosen


def cmd_queue(state, args):
    skip_rest = "--skip-rest" in args
    args = [a for a in args if a != "--skip-rest"]
    offer = state["last_offer"]
    if not offer:
        raise SystemExit("The last list is empty. Run 'scan' first.")
    nums = parse_selection(args, len(offer))
    chosen = write_batch([offer[i - 1] for i in nums])
    skipped_now = []
    if skip_rest:
        for i, k in enumerate(offer, 1):
            if i not in nums:
                state["skipped"][k] = now_str()
                skipped_now.append(k)
    save_state(state)   # numbering stays the same until the next scan
    print(f"{len(chosen)} file(s) queued:")
    for k in chosen:
        print(f"  + {short(k)}")
    for k in skipped_now:
        print(f"  – skipped: {short(k)}")


def cmd_skip(state, args):
    offer = state["last_offer"]
    nums = parse_selection(args, len(offer))
    for i in nums:
        state["skipped"][offer[i - 1]] = now_str()
        print(f"  – skipped: {short(offer[i - 1])}")
    save_state(state)


def cmd_skipped(state):
    items = sorted(state["skipped"].items(), key=lambda kv: kv[0].lower())
    if not items:
        print("No skipped files.")
    for i, (k, d) in enumerate(items, 1):
        print(f"{i:3}. {short(k)}  (skipped {d})")


def cmd_unskip(state, args):
    items = sorted(state["skipped"], key=str.lower)
    for i in parse_selection(args, len(items)):
        state["skipped"].pop(items[i - 1], None)
        print(f"  ↺ shown again: {short(items[i - 1])}")
    save_state(state)


# ─── Import of the history from the predecessor script ───────────────────────

def cmd_import_legacy(state, cache_path, note_path):
    """Legacy baseline = everything the old cache listed as still to encode, except the
    files listed in the last note of the old script. Those stay visible, as does
    everything added since."""
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    last_list = set()
    if note_path and Path(note_path).exists():
        section = False
        for line in Path(note_path).read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section = LEGACY_NOTE_HEADING in line
            elif section and line.startswith("- ["):
                last_list.add(line[6:].strip())
    baseline = set(cache.get("seen_todo", [])) - last_list
    state["baseline"] = sorted(baseline)
    save_state(state)
    print(f"Legacy baseline imported: {len(baseline)} files hidden, "
          f"{len(last_list)} from the last list stay visible "
          f"(old scan from {cache.get('last_scan')}).")


# ─── Main ─────────────────────────────────────────────────────────────────────

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
        s = scan(state, probe_files="--no-probe" not in rest)
        if "--json" in rest:
            print(json.dumps(s, indent=1, ensure_ascii=False))
            return
        print(f"Scan {s['scan']}: {s['videos_total']} videos, {s['already_h265']} already H.265")
        if not FFPROBE:
            print("Note: ffprobe not found, codec and bitrate are not read.")
        print()
        print(f"Offered ({len(s['offer'])}):")
        for it in s["offer"]:
            print(f"  {it['nr']:3}. {short(it['file'])} {it['hint']}")
        print(f"\nQueued: {len(s['queued'])}")
        print(f"Encoded, awaiting review: {len(s['awaiting_review'])}")
        print(f"Filtered (LD/TS/…): {len(s['filtered_release_type'])}")
        print(f"Hidden: {s['hidden_skipped']} skipped, {s['hidden_baseline']} legacy baseline")
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
        raise SystemExit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
