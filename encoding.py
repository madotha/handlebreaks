#!/usr/bin/env python3
"""
Handlebreaks: encoding control for a Plex library
=================================================

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
path and the path inside the Docker container (/media/...). The script uses the
first path that exists.

The library consists of shares: SHARE_1, SHARE_2, … each with a name (SHARE_1_NAME)
and an optional HandBrake preset (SHARE_1_PRESET, otherwise DEFAULT_PRESET). Files
are keyed by the share number ("share3/…"), so keep each share's number when editing
config.env. Presets live in presets/ in the data folder (see presets.py). A config.env
from version 2.x (MOVIES_DIR/SHOWS_DIR, PRESET_FILE/PRESET_NAME) is migrated
automatically on first run; the old file is kept as config.env.v2.bak.

The data folder is the folder of this script, unless the environment variable
ENCODING_DATA_DIR points elsewhere (e.g. /data in the Docker container). It holds
config.env, the database encoding.db (SQLite), queue/, processing/, done/ and logs/.
A state.json from version 1.x is imported into the database on first run.

Examples
--------
    python3 encoding.py scan
    python3 encoding.py scan --no-probe       # without ffprobe (faster)
    python3 encoding.py scan --share 3        # only share 3 (e.g. a new share)
    python3 encoding.py scan --share 3 --force   # share 3, re-read every file with ffprobe
    python3 encoding.py queue all             # everything from the last list
    python3 encoding.py queue all -3 -7 --skip-rest
                                              # everything except 3 and 7, and hide those
    python3 encoding.py queue 1,2,5-8
    python3 encoding.py queue 4 --preset my-preset   # with a different preset (id from presets/)
    python3 encoding.py skip 4
    python3 encoding.py skipped               # list hidden files
    python3 encoding.py unskip 2              # show no. 2 from "skipped" again
    python3 encoding.py status
    python3 encoding.py replace-jobs          # run pending replace jobs (normally done by encode.sh)
    python3 encoding.py purge-originals [N]   # delete kept originals older than N days (default KEEP_ORIGINALS_DAYS)
    python3 encoding.py handbrake-caps        # publish this machine's HandBrakeCLI capabilities (encode.sh)
    python3 encoding.py preset-check [--worker]   # editor/preset compatibility with this (or the worker's) HandBrakeCLI
    python3 encoding.py stop-rest <n> [--no-pause]   # requeue the run after entry n and pause, or not (encode.sh)
    python3 encoding.py version
"""

__version__ = "3.18.0"

import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import plex
import presets

# ─── Configuration ────────────────────────────────────────────────────────────

BASE = Path(__file__).resolve().parent          # program folder, e.g. ~/scripts/encoding
DATA_DIR = Path(os.environ.get("ENCODING_DATA_DIR") or BASE)
CONFIG_FILE = DATA_DIR / "config.env"           # local settings, template: config.example.env
EXAMPLE_CONFIG = BASE / "config.example.env"
DB_FILE = Path(os.environ.get("ENCODING_DB_FILE") or DATA_DIR / "encoding.db")

# For each location a list of possible paths, set from config.env (apply_config)
CONFIG = {}                                     # the settings applied by apply_config()
ROOT_CANDIDATES = {}                            # key prefix ("share3") -> paths
SHARE_NAMES = {}                                # key prefix -> display name
ENCODED_CANDIDATES = []
NOTE_DIR_CANDIDATES = []
NOTE_NAME = "Encoding To-Do.md"

# Version 1.x used German folder labels as key prefixes, version 2.x "Movies",
# "Movies 2", "Shows", … Only used to import old data (state.json, results.tsv,
# config.env), so these words must stay as they are.
LEGACY_PREFIXES = {"Filme/": "Movies/", "Serien/": "Shows/"}
V2_SECTIONS = {"Movies": "MOVIES_DIR", "Shows": "SHOWS_DIR"}
LEGACY_NOTE_HEADING = "Noch zu encoden"     # section heading in the predecessor's note (import-legacy)

STATE_FILE   = DATA_DIR / "state.json"    # only used to import data from version 1.x
SCAN_LOCK    = DATA_DIR / ".scan.lock"
MIGRATE_LOCK = DATA_DIR / ".migrate.lock"
QUEUE_DIR    = DATA_DIR / "queue"         # new batches (picked up by encode.sh)
PROC_DIR     = DATA_DIR / "processing"    # batch being encoded
DONE_DIR     = DATA_DIR / "done"          # finished batches
CANCEL_FILE  = DATA_DIR / "cancelled.txt" # "batch<TAB>key" removed from the current run (encode.sh skips them)
QUEUE_EDIT   = DATA_DIR / ".queue-edit"   # batches from queue/ while they are rewritten
STOP_FILE    = DATA_DIR / "stop.txt"      # "skip|all<TAB>key": stop the running encode (checked by encode.sh)
PAUSE_FILE   = DATA_DIR / "paused"        # queue paused by "Stop all": encode.sh starts nothing until it's gone
WORKER_LOCK  = DATA_DIR / ".lock"         # encode.sh's lock; holds "stoppable" if the worker reads STOP_FILE
REPLACE_DIR  = DATA_DIR / "replace"       # replace jobs from the web UI (run by encode.sh)
REPLACE_DONE = DATA_DIR / "replace-done"  # their results (outside replace/, which must be empty when
                                          # idle: launchd starts the worker while it has anything in it)
REPLACE_RUNNING = DATA_DIR / "replace-running"  # jobs the worker has claimed (moved out of replace/ before it
                                          # touches a file, so the web UI can cancel only jobs not started yet)
OLD_REPLACE_DONE = REPLACE_DIR / "done"   # where they were up to version 3.8
LOG_DIR      = DATA_DIR / "logs"
RESULTS_FILE = LOG_DIR / "results.tsv"
CURRENT_FILE = LOG_DIR / "current.txt"
RUN_FILE     = LOG_DIR / "run.txt"            # batch file names of the worker's current run, in order
ENCODES_FILE = LOG_DIR / "encodes.tsv"        # date, key, preset id, quality of every finished encode
PROGRESS_LOG = LOG_DIR / "progress.log"

# Find ffprobe even when launchd or another caller sets a minimal PATH
FFPROBE = shutil.which("ffprobe", path=os.pathsep.join(
    [os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]))
PROBE_WORKERS = os.cpu_count() or 4      # parallel ffprobe calls; FFPROBE_WORKERS in config.env
# Read only the start of each file: codec, resolution, duration and bitrate are in the
# headers. 30 % faster than ffprobe's default, with identical results (tested on 30 files).
PROBE_ARGS = ["-probesize", "2000000", "-analyzeduration", "0"]

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
    """Read config.env. Migrates a version 2.x config.env first, and makes sure there
    is at least one preset and a DEFAULT_PRESET."""
    if not CONFIG_FILE.exists():
        raise SystemExit(f"ERROR: {CONFIG_FILE} is missing. Copy the template: "
                         f"cp config.example.env config.env, then adjust it.")
    cfg = parse_config(CONFIG_FILE)
    if OLD_REPLACE_DONE.is_dir():
        migrate_replace_done()
    if needs_v2_migration(cfg) or not cfg.get("DEFAULT_PRESET") or not presets.all_presets():
        with file_lock(MIGRATE_LOCK):
            cfg = parse_config(CONFIG_FILE)
            if needs_v2_migration(cfg):
                migrate_v2(cfg)
            cfg = parse_config(CONFIG_FILE)
            pid = presets.bootstrap()
            if not cfg.get("DEFAULT_PRESET") or not presets.exists(cfg["DEFAULT_PRESET"]):
                write_config({"DEFAULT_PRESET": pid or presets.all_presets()[0][0]})
            cfg = parse_config(CONFIG_FILE)
    return cfg


def write_config(updates, note="removed, number stays reserved"):
    """Replace values in config.env, keeping comments and order. A value of None
    comments the setting out (with `note`), so a removed share's number stays reserved.
    New settings of a share are added next to it."""
    src = CONFIG_FILE if CONFIG_FILE.exists() else EXAMPLE_CONFIG
    lines = src.read_text(encoding="utf-8").splitlines()
    setting = lambda line: (m := re.match(r"\s*([A-Z_][A-Z0-9_]*)=", line)) and m.group(1)
    done = set()
    for i, line in enumerate(lines):
        key = setting(line)
        if key in updates:
            lines[i] = (f"# {line.strip()}   # {note}" if updates[key] is None
                        else f'{key}="{updates[key]}"')
            done.add(key)
    for key, value in updates.items():
        if key in done or value is None:
            continue
        m = re.match(r"(SHARE_\d+)", key)
        same = [i for i, line in enumerate(lines) if m and (setting(line) or "").startswith(m.group(1))]
        if not same:                            # new share: after the last share
            same = [i for i, line in enumerate(lines) if m and re.match(r"SHARE_\d+", setting(line) or "")]
        lines.insert(same[-1] + 1 if same else len(lines), f'{key}="{value}"')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_text_atomic(CONFIG_FILE, "\n".join(lines) + "\n")


def path_list(value):
    """'a:b' -> ['a', 'b'], with ~ expanded."""
    return [os.path.expanduser(p) for p in value.split(":") if p]


def shares(cfg):
    """Shares from config.env: [{"n", "prefix", "name", "paths", "preset"}], by number.
    Shares with an empty path are ignored."""
    out = []
    for n in sorted(int(m.group(1)) for k in cfg if (m := re.fullmatch(r"SHARE_(\d+)", k))):
        value = cfg.get(f"SHARE_{n}", "")
        if not value:
            continue
        paths = path_list(value)
        root = first_existing(paths)
        name = cfg.get(f"SHARE_{n}_NAME") or (root or Path(paths[0])).name or f"Share {n}"
        out.append({"n": n, "prefix": f"share{n}", "name": name, "paths": paths,
                    "preset": cfg.get(f"SHARE_{n}_PRESET", "")})
    return out


def next_share_number():
    """Next free share number. Numbers of removed shares (commented out) are never
    reused, because file keys contain them."""
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    used = [int(n) for n in re.findall(r"^#?\s*SHARE_(\d+)=", text, re.M)]
    return max([0] + used) + 1


def apply_config(cfg):
    global CONFIG, ROOT_CANDIDATES, SHARE_NAMES, ENCODED_CANDIDATES, NOTE_DIR_CANDIDATES, PROBE_WORKERS
    CONFIG = cfg
    if str(cfg.get("FFPROBE_WORKERS", "")).isdigit() and int(cfg["FFPROBE_WORKERS"]) > 0:
        PROBE_WORKERS = min(int(cfg["FFPROBE_WORKERS"]), 64)
    found = shares(cfg)
    missing = ([] if found else ["SHARE_1"]) + ([] if cfg.get("ENCODED_DIR") else ["ENCODED_DIR"])
    if missing:
        raise SystemExit(f"ERROR: {CONFIG_FILE} is missing: {', '.join(missing)}")
    ROOT_CANDIDATES = {s["prefix"]: s["paths"] for s in found}
    SHARE_NAMES = {s["prefix"]: s["name"] for s in found}
    ENCODED_CANDIDATES = path_list(cfg["ENCODED_DIR"])
    NOTE_DIR_CANDIDATES = path_list(cfg.get("NOTE_DIR", ""))


# ─── Migration from version 2.x ───────────────────────────────────────────────

def needs_v2_migration(cfg):
    return any(k in cfg for k in V2_SECTIONS.values()) and not any(re.fullmatch(r"SHARE_\d+", k) for k in cfg)


def migrate_replace_done():
    """Move the replace job results of version 3.8 and earlier from replace/done/ to
    replace-done/. While replace/ had the done/ folder in it, launchd's QueueDirectories
    started the worker every 30 seconds even with nothing to do."""
    with file_lock(MIGRATE_LOCK):
        if not OLD_REPLACE_DONE.is_dir():
            return
        REPLACE_DONE.mkdir(parents=True, exist_ok=True)
        for f in OLD_REPLACE_DONE.iterdir():
            try:
                if (REPLACE_DONE / f.name).exists():
                    f.unlink()                  # already moved (e.g. by a worker on another machine)
                else:
                    os.replace(f, REPLACE_DONE / f.name)
            except FileNotFoundError:
                continue
        try:
            OLD_REPLACE_DONE.rmdir()
        except OSError:
            pass


def migrate_v2(cfg):
    """Turn the Movies/Shows folders of version 2.x into shares (in the order Movies,
    Movies 2, …, Shows, Shows 2, …), move PRESET_FILE/PRESET_NAME into the preset store,
    and rewrite every stored key ("Movies 2/…" -> "share2/…")."""
    old = []
    for section, base in V2_SECTIONS.items():
        numbered = sorted((int(m.group(1)), k) for k in cfg
                          if (m := re.fullmatch(rf"{base}_(\d+)", k)) and int(m.group(1)) >= 2)
        for n, key in [(1, base)] + numbered:
            if cfg.get(key):
                old.append((section if n == 1 else f"{section} {n}", key))
    raw = parse_config(CONFIG_FILE, expand=False)
    prefix_map, updates = {}, {}
    for n, (prefix, key) in enumerate(old, 1):
        paths = path_list(cfg[key])
        root = first_existing(paths)
        prefix_map[prefix] = f"share{n}"
        updates[f"SHARE_{n}"] = raw[key]
        updates[f"SHARE_{n}_NAME"] = (root.name if root else prefix)
    pid = presets.bootstrap(cfg.get("PRESET_FILE") or None, cfg.get("PRESET_NAME") or None)
    if pid:
        updates["DEFAULT_PRESET"] = pid
    shutil.copy2(CONFIG_FILE, CONFIG_FILE.with_name("config.env.v2.bak"))
    write_config({k: None for k in raw if re.fullmatch(r"(MOVIES|SHOWS)_DIR(_\d+)?|PRESET_FILE|PRESET_NAME", k)},
                 note="version 2.x setting, migrated to SHARE_* and DEFAULT_PRESET")
    write_config(updates)

    with closing(connect()) as con, con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('v2_prefix_map', ?)", (json.dumps(prefix_map),))
        for table in ("files", "skipped", "known", "results"):
            for oldp, newp in prefix_map.items():
                con.execute(f"UPDATE {table} SET key = ? || substr(key, ?) WHERE key >= ? AND key < ?",
                            (newp, len(oldp) + 1, oldp + "/", oldp + "0"))
        offer = json.loads(dict(con.execute("SELECT key, value FROM meta")).get("last_offer") or "[]")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_offer', ?)",
                    (json.dumps([legacy_key(k, prefix_map) for k in offer], ensure_ascii=False),))
    for d in (QUEUE_DIR, PROC_DIR):                # batches not encoded yet
        for f in d.glob("*.txt") if d.exists() else []:
            lines = [legacy_key(l, prefix_map) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
            write_text_atomic(f, "\n".join(lines) + "\n")
    print(f"Migrated config.env to shares ({len(old)} share(s)); old file: config.env.v2.bak.", file=sys.stderr)


def first_existing(candidates):
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    return None


def reachable(path):
    """True if `path` is a folder with something in it. An unmounted share usually leaves an
    empty mount point behind (or no folder at all), which must not look like an empty share."""
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


def offline_shares():
    """Display names of the shares and the output folder that aren't reachable right now."""
    names = [SHARE_NAMES.get(p, p) for p, cands in ROOT_CANDIDATES.items()
             if not any(reachable(c) for c in cands)]
    if ENCODED_CANDIDATES and not any(Path(c).is_dir() for c in ENCODED_CANDIDATES):
        names.append("Output folder")
    return names


WORKER_WAIT_FRESH = 15 * 60    # encode.sh rewrites worker-wait.txt every ~5.5 min while it waits


def worker_wait():
    """Why the worker is waiting instead of encoding, from logs/worker-wait.txt (written by
    encode.sh, deleted once it gets going): {"kind": offline|denied|handbrake, "prefix",
    "name", "path", "error"}, or None. An old file (worker stopped meanwhile) counts as none."""
    f = LOG_DIR / "worker-wait.txt"
    try:
        if time.time() - f.stat().st_mtime > WORKER_WAIT_FRESH:
            return None
        kind, prefix, path, error = (f.read_text(encoding="utf-8").rstrip("\n").split("\t") + [""] * 4)[:4]
    except OSError:
        return None
    return {"kind": kind, "prefix": prefix, "name": SHARE_NAMES.get(prefix, prefix),
            "path": path, "error": error}


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
CREATE TABLE IF NOT EXISTS dismissed (key TEXT PRIMARY KEY, at TEXT);  -- hidden on the Status page up to this time
CREATE TABLE IF NOT EXISTS events (                             -- notifications of the web UI (webui.event_loop)
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, key TEXT, detail TEXT, level TEXT
);
CREATE TABLE IF NOT EXISTS files (                              -- inventory from the last scan
    key TEXT PRIMARY KEY, size INTEGER, mtime REAL,
    video_codec TEXT, width INTEGER, height INTEGER, duration REAL, bitrate INTEGER,
    probed_at TEXT, probe_error TEXT, status TEXT,
    added REAL                                                    -- first seen (epoch), see inventory()
);
-- narrow index for the counts of the web UI (totals, statuses, codecs, sizes, bitrates)
CREATE INDEX IF NOT EXISTS files_stats ON files (status, video_codec, size, bitrate);
"""


def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    migrate_added(con)
    migrate_state_json(con)
    return con


_ADDED_READY = set()                            # DB files already checked for the "added" column


def migrate_added(con):
    """Add files.added to a database from before it existed, filled from the file date."""
    if str(DB_FILE) in _ADDED_READY:
        return
    if "added" not in {r[1] for r in con.execute("PRAGMA table_info(files)")}:
        with con:
            con.execute("ALTER TABLE files ADD COLUMN added REAL")
            con.execute("UPDATE files SET added = mtime WHERE added IS NULL")
    con.execute("CREATE INDEX IF NOT EXISTS files_added ON files (added)")
    _ADDED_READY.add(str(DB_FILE))


def migrate_state_json(con):
    """Import state.json from version 1.x into the database, once."""
    if not STATE_FILE.exists():
        return
    old = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    pmap = stored_prefix_map(con)
    legacy = lambda k: legacy_key(k, pmap)
    with con:
        con.executemany("INSERT OR IGNORE INTO baseline VALUES (?)", [(r,) for r in old.get("baseline", [])])
        con.executemany("INSERT OR IGNORE INTO skipped VALUES (?, ?)",
                        [(legacy(k), v) for k, v in old.get("skipped", {}).items()])
        con.executemany("INSERT OR IGNORE INTO known VALUES (?)", [(legacy(k),) for k in old.get("known", [])])
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("last_offer", json.dumps([legacy(k) for k in old.get("last_offer", [])], ensure_ascii=False)),
            ("last_scan", old.get("last_scan")),
        ])
    try:
        STATE_FILE.rename(STATE_FILE.with_name("state.json.migrated"))
        print("Imported state.json into the database (old file: state.json.migrated).", file=sys.stderr)
    except FileNotFoundError:
        pass                                    # imported by another process at the same time


def legacy_key(key, prefix_map=None):
    """Translate a key from version 1.x ("Filme/…") or 2.x ("Movies 2/…") to the
    current share prefix. prefix_map is the 2.x mapping stored by migrate_v2()."""
    for old, new in LEGACY_PREFIXES.items():
        if key.startswith(old):
            key = new + key[len(old):]
            break
    head, sep, rest = key.partition("/")
    if prefix_map and sep and head in prefix_map:
        return f"{prefix_map[head]}/{rest}"
    return key


def stored_prefix_map(con):
    row = con.execute("SELECT value FROM meta WHERE key = 'v2_prefix_map'").fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def last_scan():
    """Time of the last full scan (cheaper than load_state())."""
    with closing(connect()) as con:
        row = con.execute("SELECT value FROM meta WHERE key = 'last_scan'").fetchone()
        return row[0] if row else None


def db_version():
    """Changes whenever the database changes (for caches in the web UI)."""
    out = []
    for f in (DB_FILE, DB_FILE.with_name(DB_FILE.name + "-wal")):
        try:
            st = f.stat()
            out += [st.st_mtime_ns, st.st_size]
        except FileNotFoundError:
            out += [0, 0]
    return tuple(out)


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
                "bitrate", "probed_at", "probe_error", "status", "added")


def load_files():
    """Inventory from the last scan: key -> dict with FILE_COLUMNS."""
    with closing(connect()) as con:
        rows = con.execute(f"SELECT {', '.join(FILE_COLUMNS)} FROM files")
        return {r[0]: dict(zip(FILE_COLUMNS, r)) for r in rows}


def save_files(infos, prefixes=None, path=None):
    """Replace the inventory with infos; with prefixes, only the rows of those shares;
    with path, only the rows of that file or folder."""
    with closing(connect()) as con, con:
        if path:
            con.execute("DELETE FROM files WHERE key = ? OR (key >= ? AND key < ?)",
                        (path, path + "/", path + "0"))
        elif prefixes:
            for prefix in prefixes:
                con.execute("DELETE FROM files WHERE key >= ? AND key < ?", (prefix + "/", prefix + "0"))
        else:
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


@contextmanager
def file_lock(path):
    """Exclusive lock held while the block runs (waits for other processes)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def write_text_atomic(path, text):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ─── Queue and results (shared with encode.sh) ────────────────────────────────

def cancelled_entries():
    """{(batch file name, key)} removed from the current run (see remove_queued)."""
    if not CANCEL_FILE.exists():
        return set()
    return {tuple(l.split("\t", 1)) for l in CANCEL_FILE.read_text(encoding="utf-8").splitlines() if "\t" in l}


def read_batch(f):
    """(batch file, key, preset id or "") per line of a batch file ("key" or "key<TAB>preset id")."""
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:                             # moved meanwhile (e.g. processing/ -> done/)
        return []
    return [(f, key.strip(), preset.strip()) for key, _, preset in
            (line.partition("\t") for line in lines if line.strip())]


def batch_entries():
    """(batch file, key, preset id or "") for every line in processing/ and queue/, in
    the order encode.sh works through them."""
    return [e for d in (PROC_DIR, QUEUE_DIR) if d.exists() for f in sorted(d.glob("*.txt")) for e in read_batch(f)]


def run_entries():
    """The entries of the worker's current run in the order it numbers them (current.txt),
    or None if no run is active. encode.sh lists the run's batches in run.txt when it
    starts and moves each batch to done/ as soon as it's finished, so finished batches are
    read from there: processing/ alone would shift every number after the first batch."""
    try:
        names = RUN_FILE.read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    entries = []
    for name in filter(None, (n.strip() for n in names)):
        f = PROC_DIR / name
        entries += read_batch(f) or read_batch(DONE_DIR / name)
    return entries


def run_started():
    """Start of the current run as "YYYY-MM-DD HH:MM" (results.tsv's format), or None."""
    try:
        return datetime.fromtimestamp(RUN_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return None


def queued_entries():
    """All (key, preset id or "") waiting in queue/ or processing/ to be encoded
    (without the ones removed from the current run)."""
    cancelled = cancelled_entries()
    return [(k, p) for f, k, p in batch_entries() if (f.name, k) not in cancelled]


def pending_keys():
    """Keys still to be encoded: waiting or being encoded now. Entries of the current run
    that already finished (they stay in processing/ until their batch is done) don't
    count, so such a file is offered and can be queued again."""
    return {i["key"] for i in queue_items() if i["state"] in ("waiting", "encoding")}


def import_results(con):
    """Import the lines encode.sh added to logs/results.tsv since the last import."""
    if RESULTS_FILE.exists():               # only lines added since the last import
        size = RESULTS_FILE.stat().st_size
        row = con.execute("SELECT value FROM meta WHERE key = 'results_offset'").fetchone()
        offset = int(row[0]) if row and row[0] else 0
        if size < offset:                   # the file was cleared or replaced: read it again
            offset = 0
        if size > offset:
            with open(RESULTS_FILE, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            chunk = chunk[:chunk.rfind(b"\n") + 1]   # complete lines only (encode.sh may be writing)
            pmap = stored_prefix_map(con)
            rows = [(p[0], p[1], legacy_key(p[2], pmap)) for p in (l.split("\t") for l in
                    chunk.decode("utf-8", "replace").splitlines()) if len(p) >= 3]
            with con:
                con.executemany("INSERT OR IGNORE INTO results (at, status, key) VALUES (?, ?, ?)", rows)
                con.execute("INSERT OR REPLACE INTO meta VALUES ('results_offset', ?)", (str(offset + len(chunk)),))


def load_results():
    """Latest result per file: OK, FAIL, MISSING, EXISTS or STOPPED.
    New lines from logs/results.tsv (written by encode.sh) are imported into the database first."""
    with closing(connect()) as con:
        import_results(con)
        res = {}
        for at, status, key in con.execute("SELECT at, status, key FROM results ORDER BY at, rowid"):
            res[key] = (status, at)
        return res


def clear_results():
    """Housekeeping: forget all encode results (the database table and logs/results.tsv)
    and delete finished batches, HandBrake logs of failed encodes and results of replace
    jobs. Returns the number of results removed."""
    with closing(connect()) as con, con:
        n = con.execute("DELETE FROM results").rowcount
        con.execute("DELETE FROM dismissed")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('results_offset', '0')")
    for f in (RESULTS_FILE, ENCODES_FILE):
        if f.exists():
            f.write_bytes(b"")              # truncate: encode.sh appends to the same files
    for d, pattern in ((DONE_DIR, "*.txt"), (LOG_DIR, "fail-*.log")):
        for f in d.glob(pattern) if d.exists() else []:
            f.unlink(missing_ok=True)
    unsynced = {f for f, _ in unsynced_replaces()}  # their folders still need a rescan
    for f in REPLACE_DONE.glob("*.json") if REPLACE_DONE.exists() else []:
        if f not in unsynced:
            f.unlink(missing_ok=True)
    return n


# ─── Notifications (shown by the web UI) ─────────────────────────────────────

EVENTS_KEEP_DAYS = 7                    # older notifications are deleted
EVENTS_KEEP_ROWS = 500                  # and never more than this many are kept
EVENT_FIELDS = ("id", "at", "kind", "key", "detail", "level")


def add_events(rows):
    """Store notifications: [(kind, key or None, detail or None, level or None)]. The web
    UI's event_loop() writes them, /events hands them to the pages."""
    if not rows:
        return
    at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(connect()) as con, con:
        con.executemany("INSERT INTO events (at, kind, key, detail, level) VALUES (?, ?, ?, ?, ?)",
                        [(at, *r) for r in rows])


def events_after(after, since):
    """(id of the newest notification, [notifications with an id above after and stored at or
    after since ("YYYY-MM-DD HH:MM:SS"), oldest first]). after None = only the newest id."""
    with closing(connect()) as con:
        last = con.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        rows = [] if after is None else con.execute(
            "SELECT id, at, kind, key, detail, level FROM events WHERE id > ? AND at >= ? ORDER BY id LIMIT 500",
            (after, since)).fetchall()
    return last, [dict(zip(EVENT_FIELDS, r)) for r in rows]


def prune_events():
    cutoff = (datetime.now() - timedelta(days=EVENTS_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with closing(connect()) as con, con:
        con.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        con.execute("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (EVENTS_KEEP_ROWS,))


def new_results():
    """Results that came in since the last call: [(time, status, key)], oldest first. The
    first call (no cursor stored yet) only remembers where the results end."""
    with closing(connect()) as con:
        import_results(con)
        top = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM results").fetchone()[0]
        row = con.execute("SELECT value FROM meta WHERE key = 'events_results_rowid'").fetchone()
        cursor = int(row[0]) if row and row[0].isdigit() else None
        rows = [] if cursor is None or cursor > top else con.execute(   # above top: results were cleared
            "SELECT at, status, key FROM results WHERE rowid > ? ORDER BY rowid", (cursor,)).fetchall()
        with con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('events_results_rowid', ?)", (str(top),))
    return rows


def get_meta(key):
    with closing(connect()) as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key, value):
    with closing(connect()) as con, con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def recent_results(n=20):
    """The last n results as (time, status, key), newest first."""
    load_results()
    with closing(connect()) as con:
        return con.execute("SELECT at, status, key FROM results ORDER BY at DESC, rowid DESC LIMIT ?",
                           (n,)).fetchall()


def history_keep_days():
    """HISTORY_KEEP_DAYS: how long the Status page's History shows entries (default 30, 0 = always)."""
    value = str(CONFIG.get("HISTORY_KEEP_DAYS", "30")).strip()
    return int(value) if value.isdigit() else 30


def dismiss(keys, upto=None):
    """Hide keys on the Status page: their results and finished encodes up to now, or up to
    the time given per key in upto ({key: time}). Newer results of the same file show
    again. Never touches files or results."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(k, (upto or {}).get(k, now)) for k in dict.fromkeys(keys)]
    with closing(connect()) as con, con:
        con.executemany("INSERT INTO dismissed (key, at) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET at = MAX(at, excluded.at)", rows)


def dismissed_map():
    with closing(connect()) as con:
        return dict(con.execute("SELECT key, at FROM dismissed"))


def history_entries(result="", older_than=None):
    """The Status page's History, newest first: encode results plus finished replaces
    (status REPLACED) as [{"at", "status", "key", "plex", "new_key", "can_retry"}] (plex: the Plex state of a replace,
    new_key: the key of the file that replaced it, "" for encode results). Left out: entries older
    than HISTORY_KEEP_DAYS and dismissed ones. can_retry marks a file's latest result when
    it is FAIL or STOPPED and the file isn't waiting or encoding again. older_than (days) returns the entries older than that instead
    (to dismiss them), without the age limit. result filters by status."""
    latest = load_results()
    days = history_keep_days()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M") if days else ""
    if older_than is not None:
        before, cutoff = (datetime.now() - timedelta(days=older_than)).strftime("%Y-%m-%d %H:%M"), ""
    dismissed = dismissed_map()
    pending = pending_keys()
    with closing(connect()) as con:
        rows = [(*r, None, "") for r in con.execute("SELECT at, status, key FROM results ORDER BY at DESC, rowid DESC")]
    rows += [(at, "REPLACED", key, plex_state, new_key) for at, key, plex_state, new_key in sorted(replaced_log(), reverse=True)]
    out = []
    for at, status, key, plex_state, new_key in rows:
        if result and status != result:
            continue
        if older_than is not None:
            if at >= before:
                continue
        elif (cutoff and at < cutoff) or dismissed.get(key, "") >= at:
            continue
        out.append({"at": at, "status": status, "key": key, "plex": plex_state, "new_key": new_key,
                    "can_retry": status in ("FAIL", "STOPPED") and latest.get(key) == (status, at)
                    and key not in pending})
    out.sort(key=lambda r: r["at"], reverse=True)
    return out


def last_presets(keys):
    """key -> preset id chosen when the file was queued last ("" = the share's or default
    preset), from the batch lines in done/, processing/ and queue/, newest batch first."""
    left, found = set(keys), {}
    for d in (QUEUE_DIR, PROC_DIR, DONE_DIR):
        for f in sorted(d.glob("*.txt"), reverse=True) if d.exists() else []:
            for _, key, preset in read_batch(f):
                if key in left:
                    found[key] = preset
                    left.discard(key)
            if not left:
                return found
    return found


def requeue(keys):
    """Queue files again whose latest result is FAIL or STOPPED, each with the preset it
    was queued with. Returns (keys queued, number of keys not queued)."""
    results = load_results()
    with closing(connect()) as con:
        valid = [k for k in dict.fromkeys(keys) if results.get(k, ("",))[0] in ("FAIL", "STOPPED")
                 and con.execute("SELECT 1 FROM files WHERE key = ?", (k,)).fetchone()]
    chosen = last_presets(valid)
    groups = {}
    for k in valid:
        preset = chosen.get(k) or None
        groups.setdefault(preset if preset and presets.exists(preset) else None, []).append(k)
    queued = [k for preset, ks in groups.items() for k in write_batch(ks, preset)]
    return queued, len(set(keys)) - len(queued)


def encoded_stems(enc_dir):
    """File names (without extension) of the finished files in the output folder."""
    stems = set()
    if enc_dir and enc_dir.exists():
        for f in enc_dir.rglob("*"):
            if f.is_file() and not any(p.startswith(".") for p in f.relative_to(enc_dir).parts):
                stems.add(f.stem.lower())
    return stems


# ─── Replacing originals with finished encodes ────────────────────────────────
# The web UI can't write to the library (read-only in Docker), so it only writes a job
# into replace/; encode.sh, which runs where the library is writable, carries it out
# (run_replace_jobs) and writes the result into replace-done/. Only files are touched
# here, no database, so this also works in a native worker next to a Docker web UI.

def encoded_files(enc_dir):
    """Finished files in the output folder: lowercase stem -> file name."""
    files = {}
    if enc_dir and enc_dir.exists():
        for f in enc_dir.iterdir():
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in VIDEO_EXTS:
                files[f.stem.lower()] = f.name
    return files


def delete_encoded(names):
    """Permanently delete finished encodes, given by their file name in the output folder
    (only regular video files directly in it: never .inprogress/, .replaced/ or anything
    reached by a link), with a Synology NAS's index data of the file (@eaDir/<name>).
    Returns {name: bytes freed} of the files deleted."""
    enc_dir = first_existing(ENCODED_CANDIDATES)
    if enc_dir is None:
        return {}
    deleted = {}
    for name in dict.fromkeys(names):
        if not name or "/" in name or name.startswith(".") or Path(name).suffix.lower() not in VIDEO_EXTS:
            continue
        path = enc_dir / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        deleted[name] = size
        shutil.rmtree(enc_dir / "@eaDir" / name, ignore_errors=True)
    return deleted


def unwait(keys):
    """After their encodes were deleted, files that showed as "awaiting review" are offered
    again (or skipped, if they were skipped); the next scan classifies them completely."""
    skipped = set(load_state()["skipped"])
    with closing(connect()) as con, con:
        con.executemany("UPDATE files SET status = ? WHERE key = ? AND status = 'waiting'",
                        [("skipped" if k in skipped else "offer", k) for k in keys])


def encode_presets():
    """{key: preset id} of the latest finished encode of each file (logs/encodes.tsv)."""
    found = {}
    if ENCODES_FILE.exists():
        for line in ENCODES_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2]:
                found[parts[1]] = parts[2]
    return found


# Codec tag in release names ("…WEB.h264-GROUP", "x264", "H.264", "AVC", "XviD" …), matched
# as a separator-delimited token. suggest_name() replaces the last one with the encode's codec.
OLD_CODEC_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[xh]\.?26[45]|avc|hevc|xvid|divx|mpeg-?[24]|vc-?1|wmv[0-9]?|av1|vp9)(?![A-Za-z0-9])",
    re.IGNORECASE)
DEFAULT_CODEC_TAGS = {"hevc": "x265", "av1": "AV1", "vvc": "VVC", "h264": "x264"}
GROUP_RE = re.compile(r"-[A-Za-z0-9]+(?=$|[\s.\[{(])")   # release group right after the codec tag
# Release group at the end of a name, optionally followed by Plex tags: "…-LAMA {edition-Arabic}"
END_GROUP_RE = re.compile(r"-[A-Za-z0-9]+(?=(?:\s*[\[{(][^\]})]*[\]})])*\s*$)")


def codec_tags():
    """ffprobe codec name -> tag in file names; RENAME_CODEC_TAGS ("hevc=x265 av1=AV1") overrides."""
    tags = dict(DEFAULT_CODEC_TAGS)
    for pair in str(CONFIG.get("RENAME_CODEC_TAGS", "")).split():
        codec, _, tag = pair.partition("=")
        if codec and tag:
            tags[codec.lower()] = tag
    return tags


def encode_quality(key):
    """Quality of the finished encode of key ("rf20", "avg2000"), or None. encode.sh records it
    in encodes.tsv; for older encodes the preset is looked up: the one chosen when queueing
    (latest batch with the key), the share's, the default."""
    if ENCODES_FILE.exists():
        found = None
        for line in ENCODES_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 4 and parts[1] == key and parts[3]:
                found = parts[3]
        if found:
            return found
    preset_id = None
    for d in (DONE_DIR, PROC_DIR, QUEUE_DIR):
        for f in sorted(d.glob("*.txt"), reverse=True) if d.exists() else []:
            for _, k, p in read_batch(f):
                if k == key:
                    preset_id = p
                    break
            if preset_id is not None:
                break
        if preset_id is not None:
            break
    if not preset_id:
        preset_id = CONFIG.get(f"SHARE_{key.split('/', 1)[0][5:]}_PRESET") or CONFIG.get("DEFAULT_PRESET")
    try:
        return presets.quality_tag(presets.load(preset_id)) if preset_id else None
    except Exception:
        return None


def suggest_name(original_name, encoded_name, codec=None, quality=None):
    """Suggested name for an encode replacing original_name: the last codec tag (h264,
    x264, H.264, AVC, XviD …) becomes the tag of the encode's codec (codec_tags(); an
    unknown codec counts as hevc), and with RENAME_GROUP set, the release group right
    after it ("-NTB") becomes "-<RENAME_GROUP>". With a quality ("rf20", "avg2000") and
    RENAME_QUALITY on (default), it follows the release group: "x265-donm.rf20". A name
    without a codec tag but with a release group at the end gets the tag before the group
    ("WEBRip-LAMA" -> "WEBRip.x265-LAMA.rf20"); other names stay as they are. The extension
    follows the encode."""
    stem = Path(original_name).stem
    ext = Path(encoded_name).suffix
    m = None
    for m in OLD_CODEC_RE.finditer(stem):
        pass
    tag = codec_tags().get((codec or "hevc").lower())
    group = str(CONFIG.get("RENAME_GROUP", "")).strip().strip("-")
    q = f".{quality}" if quality and str(CONFIG.get("RENAME_QUALITY", "1")).strip() != "0" else ""
    if m and tag:
        rest = stem[m.end():]
        g = GROUP_RE.match(rest)
        if g:                                   # codec-group[.quality]
            rest = ("-" + group if group else g.group(0)) + q + rest[g.end():]
        else:
            rest = q + rest
        stem = stem[:m.start()] + tag + rest
    elif tag and (g := END_GROUP_RE.search(stem)):
        stem = stem[:g.start()] + "." + tag + ("-" + group if group else g.group(0)) + q + stem[g.end():]
    return stem + ext


def check_new_name(new_name, encoded_name):
    """Problem with the chosen file name, or None. The extension must stay the encoded
    file's, because it matches its container."""
    ext = Path(encoded_name).suffix
    if not new_name or "/" in new_name or "\0" in new_name or new_name.startswith("."):
        return "Enter a file name without folders."
    if Path(new_name).suffix.lower() != ext.lower() or not Path(new_name).stem.strip():
        return f"The name must end with {ext} (the container of the encoded file)."
    if len(new_name.encode()) > 255:
        return "The name is too long."
    return None


def request_replace(key, encoded_name, new_name, keep_original=True):
    """Write a replace job; returns its id."""
    problem = check_new_name(new_name, encoded_name)
    if problem:
        raise SystemExit(problem)
    REPLACE_DIR.mkdir(parents=True, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    n = 0
    while (REPLACE_DIR / f"{job_id}.json").exists() or (REPLACE_DONE / f"{job_id}.json").exists():
        n += 1                                  # several jobs in the same microsecond (bulk replace)
        job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{n}"
    job = {"id": job_id, "key": key, "encoded": encoded_name, "new_name": new_name,
           "keep_original": bool(keep_original), "requested": now_str()}
    tmp = DATA_DIR / f".replace-{job_id}.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, REPLACE_DIR / f"{job_id}.json")
    return job_id


PLEX_UNCONFIRMED = ("pending", "scanning", "failed")


def replace_status(job_id):
    """Result of a replace job: {"state": "pending" | "done" | "failed" | "cancelled", "message", …};
    the message ends with the Plex state."""
    if not re.fullmatch(r"[\d-]+", job_id or ""):
        return {"state": "failed", "message": "Unknown job."}
    done = REPLACE_DONE / f"{job_id}.json"
    if done.exists():
        job = json.loads(done.read_text(encoding="utf-8"))
        if job.get("plex"):
            job["message"] = f"{job['message']} {plex.note(job['plex'])}"
        return job
    if (REPLACE_DIR / f"{job_id}.json").exists():
        return {"state": "pending", "message": "Waiting for the worker …"}
    if (REPLACE_RUNNING / f"{job_id}.json").exists():
        return {"state": "pending", "started": True, "message": "The worker is moving the files …"}
    return {"state": "failed", "message": "Unknown job."}


def open_replaces():
    """{key: (job id, started)} of the replace jobs the worker hasn't finished: waiting in
    replace/ (started False: can still be cancelled) or claimed in replace-running/."""
    jobs = {}
    for folder, started in ((REPLACE_DIR, False), (REPLACE_RUNNING, True)):
        for f in sorted(folder.glob("*.json")) if folder.exists() else []:
            try:
                jobs[json.loads(f.read_text(encoding="utf-8"))["key"]] = (f.stem, started)
            except (ValueError, KeyError, OSError):
                continue
    return jobs


def pending_replaces():
    """Keys with a replace job the worker hasn't carried out yet."""
    return set(open_replaces())


def cancel_replace(job_id):
    """Cancel a replace job the worker hasn't started. The job file is moved out of replace/
    in one rename; the worker claims a job the same way (run_replace_jobs), so exactly one of
    them gets it. The result says "cancelled" (and counts as synced: nothing to scan).
    Returns None, or why it couldn't be cancelled."""
    if not re.fullmatch(r"[\d-]+", job_id or ""):
        return "Unknown job."
    taken = DATA_DIR / f".replace-cancel-{job_id}.tmp"
    try:
        os.replace(REPLACE_DIR / f"{job_id}.json", taken)
    except FileNotFoundError:
        if (REPLACE_RUNNING / f"{job_id}.json").exists():
            return "The worker has already started moving the files; it can't be cancelled any more."
        if (REPLACE_DONE / f"{job_id}.json").exists():
            return "The replace has already finished."
        return "Unknown job."
    try:
        job = json.loads(taken.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        job = {"id": job_id, "key": ""}
    REPLACE_DONE.mkdir(parents=True, exist_ok=True)
    write_text_atomic(REPLACE_DONE / f"{job_id}.json", json.dumps(
        dict(job, state="cancelled", message="Cancelled before the worker started; no file was moved.",
             finished=now_str(), synced=True), ensure_ascii=False))
    taken.unlink(missing_ok=True)
    return None


def cancel_replaces(ids=None, keys=None):
    """Cancel the waiting replace jobs with these ids or for these keys (both None: all
    waiting ones). Returns (cancelled, number that had already started or finished)."""
    done = late = 0
    for key, (job_id, started) in open_replaces().items():
        if (ids is not None or keys is not None) and job_id not in (ids or ()) and key not in (keys or ()):
            continue
        if started or cancel_replace(job_id):
            late += 1
        else:
            done += 1
    return done, late


def unsynced_replaces():
    """Finished replace jobs whose result isn't in the database yet: [(file, job)].
    The worker only moves files (it may run on another machine than the database), so
    the web UI rescans the folder of each one and then marks it with sync_replace()."""
    jobs = []
    for f in sorted(REPLACE_DONE.glob("*.json")) if REPLACE_DONE.exists() else []:
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not job.get("synced"):
            jobs.append((f, job))
    return jobs


def mark_replace_synced(path, job):
    update_replace_result(path, synced=True)


_REPLACED_LOG = [None, []]                      # [replace-done/ mtime, [(finished, key, plex state, new key)]]


def replaced_log():
    """[(finished time, key, Plex state or None, key of the replacing file)] of finished
    replace jobs, read again only when replace-done/ changes."""
    try:
        stamp = REPLACE_DONE.stat().st_mtime_ns
    except OSError:
        return []
    if _REPLACED_LOG[0] != stamp:
        log = []
        for f in REPLACE_DONE.glob("*.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if job.get("state") == "done":
                log.append((job.get("finished") or "", job["key"], (job.get("plex") or {}).get("state"),
                            job.get("new_key") or ""))
        _REPLACED_LOG[:] = [stamp, log]
    return _REPLACED_LOG[1]


def replaced_keys():
    """Keys whose original was replaced by a finished replace job."""
    return {key for _, key, _, _ in replaced_log()}


_REPLACE_RECORDS = [None, {}]


def replace_records():
    """{original key: (finished time, new key, Plex state, [original, replacement] bytes or None)} of
    the finished replace jobs, the latest per key; read again only when replace-done/ changes."""
    try:
        stamp = REPLACE_DONE.stat().st_mtime_ns
    except OSError:
        return {}
    if _REPLACE_RECORDS[0] != stamp:
        records = {}
        for f in REPLACE_DONE.glob("*.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if job.get("state") == "done" and job.get("keep_original") is not False:
                when = job.get("finished") or ""
                if job["key"] not in records or when >= records[job["key"]][0]:
                    records[job["key"]] = (when, job.get("new_key") or "", (job.get("plex") or {}).get("state"),
                                           job.get("sizes"))
        _REPLACE_RECORDS[:] = [stamp, records]
    return _REPLACE_RECORDS[1]


def kept_dir():
    """The folder with the originals that Replace… kept (<output folder>/.replaced), or None."""
    enc_dir = first_existing(ENCODED_CANDIDATES)
    return enc_dir / ".replaced" if enc_dir else None


def kept_originals():
    """The originals Replace… kept, newest first: [{"key": path inside .replaced/ (the same
    as the original's path in its share), "size", "kept" (when it was kept, "YYYY-MM-DD
    HH:MM"), "orig_key", "plex" (Plex state of the replace), "new_key" (of the replacing file; "" if there is no record of the
    replace), "saved" (bytes the replace saved, None if it wasn't recorded: replaces before 3.13)}]. The share isn't part of the path, so with the same path in two shares the
    latest replace job is taken."""
    base = kept_dir()
    if base is None or not base.is_dir():
        return []
    by_path = {}
    for key, (when, new_key, plex_state, sizes) in replace_records().items():
        rel = key.split("/", 1)[-1]
        if rel not in by_path or when >= by_path[rel][0]:
            by_path[rel] = (when, key, new_key, plex_state, sizes)
    out = []
    for folder, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d != "@eaDir"]        # Synology's index data, not originals
        for name in names:
            if Path(name).suffix.lower() not in VIDEO_EXTS:   # only the video files Replace… moved
                continue
            path = Path(folder) / name
            try:
                if path.is_symlink():           # never follow links out of the folder
                    continue
                st = path.stat()
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            when, orig_key, new_key, plex_state, sizes = by_path.get(rel, (None, "", "", None, None))
            out.append({"key": rel, "size": st.st_size, "orig_key": orig_key, "new_key": new_key, "plex": plex_state,
                        "saved": sizes[0] - sizes[1] if sizes and len(sizes) == 2 else None,
                        "kept": when or datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M")})
    return sorted(out, key=lambda o: (o["kept"], o["key"]), reverse=True)


def delete_kept(rels):
    """Permanently delete kept originals, given by their path inside .replaced/ (anything
    that isn't a file there is ignored), together with a Synology NAS's index data of the
    file (@eaDir/<name>), and remove the folders left empty.
    Returns (files deleted, bytes freed)."""
    base = kept_dir()
    if base is None or not base.is_dir():
        return 0, 0
    base_real = base.resolve()
    count = freed = 0
    for rel in dict.fromkeys(rels):
        path = base / rel
        try:
            if not path.resolve().is_relative_to(base_real) or not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        count += 1
        freed += size
        shutil.rmtree(path.parent / "@eaDir" / path.name, ignore_errors=True)   # a Synology NAS's index data of this file
        folder = path.parent
        try:
            (folder / "@eaDir").rmdir()          # if that was its last entry
        except OSError:
            pass
        while folder != base and folder.is_relative_to(base):    # empty folders up to .replaced/
            try:
                folder.rmdir()
            except OSError:
                break
            folder = folder.parent
    return count, freed


def keep_originals_days():
    """KEEP_ORIGINALS_DAYS: after how many days kept originals are deleted (0 = never, the default)."""
    value = str(CONFIG.get("KEEP_ORIGINALS_DAYS", "0")).strip()
    return int(value) if value.isdigit() else 0


KEPT_BLOCKED = ("missing", "offline")    # replacement states that keep an original from being deleted


def replacement_state(new_key, roots=None):
    """(state, size) of the file that replaced a kept original: "ok" with its size, "missing"
    (no longer in the library: renamed, moved or deleted), "offline" (its share can't be
    reached, so it can't be checked) or "unknown" (no record of the replace, e.g. the history
    was deleted). roots caches the reachable root per share across calls."""
    if not new_key:
        return "unknown", None
    prefix, _, rel = new_key.partition("/")
    roots = {} if roots is None else roots
    if prefix not in roots:
        roots[prefix] = next((Path(c) for c in ROOT_CANDIDATES.get(prefix, []) if reachable(c)), None)
    if roots[prefix] is None:
        return "offline", None
    try:
        return "ok", (roots[prefix] / rel).stat().st_size
    except OSError:
        return "missing", None


def split_kept(items, force=False):
    """(deletable, held back) kept originals (dicts of kept_originals()): one whose replacement
    is missing or can't be checked is held back unless force."""
    if force:
        return list(items), []
    roots, ok, held = {}, [], []
    for o in items:
        (held if replacement_state(o["new_key"], roots)[0] in KEPT_BLOCKED else ok).append(o)
    return ok, held


def purge_kept(days=None):
    """Delete the kept originals that were kept for more than `days` days (default:
    KEEP_ORIGINALS_DAYS; 0 = nothing). Returns (files deleted, bytes freed, held back): an
    original whose replacement Plex hasn't confirmed, or whose replacement is missing or
    can't be checked, stays until it's deleted by hand."""
    days = keep_originals_days() if days is None else days
    if days <= 0:
        return 0, 0, 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    ok, held = split_kept(o for o in kept_originals() if o["kept"] < cutoff and o["plex"] not in PLEX_UNCONFIRMED)
    return (*delete_kept([o["key"] for o in ok]), len(held))


def move_file(src, dst, attempts=6, wait=10):
    """Move a file, retrying while a NAS briefly refuses it (e.g. its media indexer)."""
    for attempt in range(1, attempts + 1):
        try:
            shutil.move(str(src), str(dst))     # rename, or copy + delete across file systems
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(wait)


def replace_one(job):
    root = first_existing(ROOT_CANDIDATES.get(job["key"].split("/", 1)[0], []))
    enc_dir = first_existing(ENCODED_CANDIDATES)
    if root is None or enc_dir is None:
        raise RuntimeError("The share or the output folder isn't reachable.")
    rel = job["key"].split("/", 1)[1]
    original = root / rel
    encoded = enc_dir / job["encoded"]
    target = original.parent / job["new_name"]
    if check_new_name(job["new_name"], job["encoded"]) or not target.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Invalid file name.")
    if not encoded.is_file():
        raise RuntimeError(f"The encoded file {job['encoded']} isn't in the output folder any more.")
    if target.exists() and target != original:
        raise RuntimeError(f"{job['new_name']} already exists in that folder. Choose another name.")
    if not os.access(original.parent, os.W_OK):
        raise RuntimeError(f"No permission to write to the folder {original.parent}. Check its owner and "
                           "permissions on the share (e.g. a folder created by another user or program); "
                           "in Docker, the library is also mounted read-only unless you change that.")
    backup = enc_dir / ".replaced" / rel if job["keep_original"] and original.exists() else None
    if backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
    if target != original:
        # A new name: put the new file in first and take the original out afterwards, so
        # Plex (or anything else scanning the folder) never sees the item without a file.
        move_file(encoded, target)
        if original.exists():
            try:
                if backup:
                    move_file(original, backup)
                else:
                    original.unlink()
            except Exception:
                move_file(target, encoded)      # undo: the original stays where it was
                raise
    else:
        # The same name: bring the new file into the folder under a hidden temporary name,
        # then swap it in with one rename. The original is kept by a second (hard) link, so
        # the file is never missing; across file systems it is moved, with a short gap.
        tmp = target.with_name(f".{target.name}.handlebreaks-tmp")
        move_file(encoded, tmp)
        linked = False
        try:
            if backup:
                try:
                    os.link(original, backup)
                    linked = True
                except OSError:
                    move_file(original, backup)
            os.replace(tmp, target)
        except Exception:
            if linked:
                backup.unlink(missing_ok=True)
            elif backup and backup.exists() and not original.exists():
                move_file(backup, original)     # put the original back
            if tmp.exists():
                move_file(tmp, encoded)
            raise
    kept = f" The original is kept in {backup}." if backup else " The original was deleted."
    return (f"Replaced {original.name} with {target.name}.{kept}",
            f"{job['key'].split('/', 1)[0]}/" + str(target.relative_to(root)), original, target)


def run_replace_jobs():
    """Carry out all pending replace jobs (file moves only). Plex is told by the web UI,
    which scans each folder once for the replaces that came together and checks the result
    (plex.advance()); the result holds what it needs (plex.job_info()). Returns how many ran.
    Each job is first claimed: moved from replace/ into replace-running/ in one rename, so a
    cancel from the web UI (cancel_replace) either wins before any file is touched or fails.
    Jobs left in replace-running/ by a worker that died are carried out again (replace_one
    never overwrites a file, so a half-done one fails cleanly)."""
    with file_lock(DATA_DIR / ".replace.lock"):
        return _run_replace_jobs()


def _run_replace_jobs():
    leftover = sorted(REPLACE_RUNNING.glob("*.json")) if REPLACE_RUNNING.exists() else []
    jobs = leftover + (sorted(REPLACE_DIR.glob("*.json")) if REPLACE_DIR.exists() else [])
    if jobs:
        REPLACE_DONE.mkdir(parents=True, exist_ok=True)
        REPLACE_RUNNING.mkdir(parents=True, exist_ok=True)
    ran = 0
    for path in jobs:
        if path.parent != REPLACE_RUNNING:
            try:
                os.replace(path, REPLACE_RUNNING / path.name)
            except FileNotFoundError:           # cancelled in the web UI meanwhile
                continue
            path = REPLACE_RUNNING / path.name
        ran += 1
        job = json.loads(path.read_text(encoding="utf-8"))
        prefix, _, rel = job["key"].partition("/")
        root = first_existing(ROOT_CANDIDATES.get(prefix, []))
        try:
            old_size = (root / rel).stat().st_size if root else None
        except OSError:
            old_size = None
        try:
            message, new_key, original, target = replace_one(job)
            result = dict(job, state="done", message=message, new_key=new_key, finished=now_str())
            try:
                size = target.stat().st_size
            except OSError:
                size = None
            if old_size and size:
                result["sizes"] = [old_size, size]      # original and replacement, for the saved space
            info = plex.job_info(CONFIG, original, target, size)
            if info:
                result["plex"] = info
        except Exception as e:
            result = dict(job, state="failed", message=f"Not replaced: {e}", finished=now_str())
        print(f"{now_str()} replace {job['key']}: {result['message']}")
        # the result is written right away, so a job never runs twice
        write_text_atomic(REPLACE_DONE / path.name, json.dumps(result, ensure_ascii=False))
        path.unlink(missing_ok=True)
    return ran


_RESULT_LOCK = threading.Lock()


def update_replace_result(path, **fields):
    """Change fields of a finished job's result, re-reading it first so a change made
    meanwhile (the web UI's threads set synced and plex) isn't lost."""
    with _RESULT_LOCK:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        write_text_atomic(path, json.dumps(dict(saved, **fields), ensure_ascii=False))


def plex_open_jobs():
    """[(result file, job)] of finished replaces whose Plex update isn't settled yet."""
    jobs = []
    for f in sorted(REPLACE_DONE.glob("*.json")) if REPLACE_DONE.exists() else []:
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if (job.get("plex") or {}).get("state") in ("pending", "scanning"):
            jobs.append((f, job))
    return jobs


# ─── Scan ─────────────────────────────────────────────────────────────────────

def under(key, path):
    """True if the key is the file `path` or lies in the folder `path`."""
    return key == path or key.startswith(path + "/")


def walk_library(prefixes=None, path=None):
    """Yields (key, relative_path, file_name, full_path) for every video (of the given
    shares only, if prefixes is set; only the file or folder `path`, a key, if set)."""
    missing = []
    for label, cands in ROOT_CANDIDATES.items():
        if prefixes and label not in prefixes:
            continue
        root = first_existing(cands)
        if root is None or not reachable(root):   # an empty root is most likely unmounted
            missing.append(label)
            continue
        start = root
        if path:
            start = root / path.split("/", 1)[1] if "/" in path else root
            if ".." in Path(path).parts or not start.resolve().is_relative_to(root.resolve()):
                raise SystemExit(f"ERROR: {path} is outside the share.")
            if start.is_file():                 # a single file
                if not start.name.startswith(".") and start.suffix.lower() in VIDEO_EXTS:
                    rel = str(start.relative_to(root))
                    yield f"{label}/{rel}", rel, start.name, start
                continue
        for dirpath, dirnames, filenames in os.walk(start):   # nothing if the folder is gone
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
        names = ", ".join(SHARE_NAMES.get(m, m) for m in missing)
        raise SystemExit(f"ERROR: {names} not reachable. Is the NAS mounted?")


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
            [FFPROBE, "-v", "error", *PROBE_ARGS, "-select_streams", "v:0",
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


CHECKPOINT_EVERY = 200      # files probed between saves, so an interrupted scan keeps its progress


def save_checkpoint(infos):
    """Store freshly probed files during a scan. Known rows only get the new size, time and
    probe data (status stays); new rows have no status until the scan finishes. The next
    scan then finds them in the cache and resumes."""
    cols = [c for c in FILE_COLUMNS if c not in ("key", "status")]
    with closing(connect()) as con, con:
        con.executemany(
            f"INSERT INTO files (key, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))}) "
            f"ON CONFLICT(key) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols if c != 'added')}",
            [(i["key"], *(i.get(c) for c in cols)) for i in infos])


def inventory(entries, probe_files, force=False, first_scan=()):
    """Size, modification time and (if wanted) ffprobe data per file.
    ffprobe only runs for new or changed files and files whose last probe failed (all files
    with force); the rest comes from the database. "added" is when a file was first seen: kept from the database,
    the scan time for a new file, or its modification date on the first scan of its
    share (prefix in first_scan), when "now" would say nothing."""
    cached = load_files()
    now = time.time()
    infos, todo = {}, []
    for key, _, _, full in entries:
        try:
            st = full.stat()
        except OSError:
            continue
        old = cached.get(key)
        added = old["added"] if old and old["added"] else \
            min(st.st_mtime, now) if key.split("/", 1)[0] in first_scan else now
        info = {"key": key, "size": st.st_size, "mtime": st.st_mtime, "added": added}
        if not force and old and old["size"] == st.st_size and old["mtime"] == st.st_mtime and old["probed_at"] \
                and not (old["probe_error"] and probe_files and FFPROBE):   # failed probes are retried
            info.update({c: old[c] for c in ("video_codec", "width", "height", "duration",
                                              "bitrate", "probed_at", "probe_error")})
        elif probe_files and FFPROBE:
            todo.append((key, full))
        infos[key] = info

    if todo:
        print(f"ffprobe: {len(todo)} new, changed or previously failed file(s) …", file=sys.stderr)
        with ThreadPoolExecutor(PROBE_WORKERS) as pool:
            for start in range(0, len(todo), CHECKPOINT_EVERY):
                chunk = todo[start:start + CHECKPOINT_EVERY]
                for (key, _), data in zip(chunk, pool.map(probe, [f for _, f in chunk])):
                    infos[key].update(data)
                save_checkpoint([infos[k] for k, _ in chunk])
                print(f"ffprobe: {start + len(chunk)}/{len(todo)}", file=sys.stderr)
    return infos


def scan(state, probe_files=True, only=None, force=False, path=None):
    """Scan all shares, or only the shares whose prefixes are in `only` (the others keep
    the data of their last scan), or only one file or folder (`path`, a key such as
    "share6/Show/Season 1"). force re-reads every scanned file with ffprobe."""
    if path:
        only = [path.split("/", 1)[0]]
    with scan_lock():
        return _scan(state, probe_files, only, force, path)


def share_scans():
    """Key prefix -> time of the last scan of that share."""
    with closing(connect()) as con:
        return {k[len("share_scan:"):]: v for k, v in con.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'share_scan:%'")}


def _scan(state, probe_files, only=None, force=False, path=None):
    enc_dir = first_existing(ENCODED_CANDIDATES)
    done_stems = encoded_stems(enc_dir)
    in_queue = pending_keys()                   # not the finished entries of the current run: they leave the queue
    results = load_results()
    baseline = set(state["baseline"])
    skipped = state["skipped"]
    known = set(state["known"])

    entries = list(walk_library(only, path))
    scanned_before = share_scans()
    infos = inventory(entries, probe_files, force,
                      first_scan={p for p in ROOT_CANDIDATES if p not in scanned_before})

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
    save_files(infos, only, path)
    if not path:                                # a share counts as scanned only when scanned completely
        scanned = only or list(ROOT_CANDIDATES)
        with closing(connect()) as con, con:
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            [(f"share_scan:{p}", now_str()) for p in scanned])
    if only:                                    # the list covers all shares: others from their last scan
        offer, waiting, queued, bad, hid_skip, hid_base = [], [], [], [], [], []
        n_total = n_encoded = 0
        lists = {"offer": offer, "waiting": waiting, "queued": queued, "bad": bad,
                 "skipped": hid_skip, "baseline": hid_base}
        for key, info in load_files().items():
            n_total += 1
            n_encoded += info["status"] == "h265"
            if info["status"] in lists:
                lists[info["status"]].append(key)

    offer.sort(key=str.lower)
    flags = {}
    for k in offer:
        r = results.get(k)
        if r and r[0] == "FAIL":
            flags[k] = "⚠️ last encode failed"
        elif k not in known:
            flags[k] = "🆕"

    # Forget skipped files that no longer exist (in the scanned shares, file or folder)
    state["skipped"] = {k: v for k, v in skipped.items()
                        if k in all_unencoded or (only and k.split("/", 1)[0] not in only)
                        or (path and not under(k, path))}
    state["known"] = sorted(known | set(offer))
    state["last_offer"] = offer
    if not path:                                # a file/folder check doesn't reset the rescan timer
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
    """Short, readable form: share name, folder of the show/movie and file name."""
    parts = key.split("/")
    share = SHARE_NAMES.get(parts[0], parts[0])
    if len(parts) > 2:
        return f"{share}: {parts[1]} › {parts[-1]}"
    return f"{share}: {parts[-1]}" if len(parts) == 2 else key


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
        f"*As of the scan. For live status, use the Handlebreaks web UI or `status.sh`.*",
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


# State of a queued entry (queue_items) -> label; the upper-case ones are results of encode.sh
QUEUE_STATES = {"encoding": "Encoding", "waiting": "Waiting", "removed": "Removed", "OK": "Done", "FAIL": "Failed",
                "STOPPED": "Stopped", "skipped": "Skipped (see log)",
                "MISSING": "Missing in library", "EXISTS": "Output already exists"}


def queue_items():
    """Every entry of the current run and the queue, in the order encode.sh works through
    them: [{"key", "preset", "state", "batch", "run"}] ("run": the entry belongs to the
    worker's current run, otherwise it starts with the next one). The run's entries come from run_entries()
    and are numbered like encode.sh does (current.txt holds the number of the file being
    encoded): earlier entries are finished (state = their result from this run, or
    "skipped" if there is none), the current one is "encoding", the rest "waiting".
    Batches in processing/ without an active run and everything in queue/ are "waiting"."""
    cur = current_progress()
    current = int(cur["i"]) if cur and cur["i"].isdigit() else None
    cancelled = cancelled_entries()
    run = run_entries()
    items = []
    if run is not None:
        results = load_results()
        since = run_started() or ""
        for n, (f, key, preset) in enumerate(run, 1):
            if (f.name, key) in cancelled:
                state = "removed"               # still counted: encode.sh numbers it, then skips it
            elif current and n < current:
                result = results.get(key)       # only a result of this run counts
                state = result[0] if result and result[1] >= since else "skipped"
            elif current and n == current:
                state = "encoding"
            else:
                state = "waiting"
            items.append({"key": key, "preset": preset, "state": state, "batch": f, "run": True})
        rest = [e for e in batch_entries() if e[0].parent == QUEUE_DIR]
    else:
        rest = batch_entries()
    items += [{"key": key, "preset": preset, "state": "waiting", "batch": f, "run": False} for f, key, preset in rest]
    return items


def take_out_of_queue(waiting):
    """Take these waiting items (from queue_items()) out of the batches they are in:
    batches in queue/ are rewritten, entries of the current run (processing/, already
    counted by encode.sh) go to the cancel list, which encode.sh checks before each file
    and clears when the run is over."""
    cancel = [(i["batch"].name, i["key"]) for i in waiting if i["batch"].parent == PROC_DIR]
    drop = {}
    for i in waiting:
        if i["batch"].parent == QUEUE_DIR:
            drop.setdefault(i["batch"], set()).add(i["key"])
    QUEUE_EDIT.mkdir(parents=True, exist_ok=True)
    for batch, ks in drop.items():
        edit = QUEUE_EDIT / batch.name
        try:
            os.replace(batch, edit)             # out of the worker's sight while it is rewritten
        except FileNotFoundError:               # the worker just took it over: cancel instead
            cancel += [(batch.name, k) for k in ks]
            continue
        lines = [l for l in edit.read_text(encoding="utf-8").splitlines()
                 if l.strip() and l.partition("\t")[0].strip() not in ks]
        if lines:
            edit.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
            os.replace(edit, batch)
        else:
            edit.unlink()
    if cancel:
        with open(CANCEL_FILE, "a", encoding="utf-8") as f:
            f.writelines(f"{b}\t{k}\n" for b, k in cancel)


def remove_queued(keys=None):
    """Remove waiting entries from the queue: all of them, or the given keys.
    Returns the keys removed."""
    wanted = None if keys is None else set(keys)
    waiting = [i for i in queue_items() if i["state"] == "waiting" and (wanted is None or i["key"] in wanted)]
    take_out_of_queue(waiting)
    removed = list(dict.fromkeys(i["key"] for i in waiting))
    unqueue_status(removed)
    return removed


MOVES = ("top", "bottom", "up", "down", "before")


def moved_order(waiting, op, keys, target=""):
    """The waiting keys (in queue order) after moving `keys` to the top or bottom, one
    step up or down, or in front of `target` ("" = to the end). The moved keys keep their
    order among themselves; keys that aren't waiting are ignored."""
    chosen = set(keys)
    sel = [k for k in waiting if k in chosen]
    rest = [k for k in waiting if k not in chosen]
    out = list(waiting)
    if not sel:
        return out
    if op == "top":
        return sel + rest
    if op == "bottom":
        return rest + sel
    if op == "before":
        if target in chosen:
            return out
        at = rest.index(target) if target in rest else len(rest)
        return rest[:at] + sel + rest[at:]
    if op == "up":
        for n in range(1, len(out)):
            if out[n] in chosen and out[n - 1] not in chosen:
                out[n - 1], out[n] = out[n], out[n - 1]
    elif op == "down":
        for n in range(len(out) - 2, -1, -1):
            if out[n] in chosen and out[n + 1] not in chosen:
                out[n], out[n + 1] = out[n + 1], out[n]
    return out


def reorder_queued(order):
    """Put the waiting entries in a new order (all their keys, see moved_order). The worker
    works through its run's batch files as it read them, so entries from the first changed
    position on are taken out of their batches (take_out_of_queue) and written as one new
    batch in queue/, which the next run starts with; the entries before it stay where they
    are. The new batch is staged in .queue-edit/ until the old ones are rewritten, so the
    worker never sees an entry twice. Presets chosen when queueing are kept.
    Returns the number of entries that got a new place."""
    waiting = [i for i in queue_items() if i["state"] == "waiting"]
    current = list(dict.fromkeys(i["key"] for i in waiting))
    order = [k for k in dict.fromkeys(order) if k in current]
    order += [k for k in current if k not in order]
    first = next((n for n, (a, b) in enumerate(zip(current, order)) if a != b), None)
    if first is None:
        return 0
    tail = order[first:]
    presets_of = {}
    for i in waiting:
        presets_of.setdefault(i["key"], i["preset"])
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_EDIT.mkdir(parents=True, exist_ok=True)
    name = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.txt"   # sorts after the batches in queue/
    staged = QUEUE_EDIT / name
    staged.write_text("".join(f"{k}\t{presets_of[k]}\n" if presets_of[k] else f"{k}\n" for k in tail), encoding="utf-8")
    take_out_of_queue([i for i in waiting if i["key"] in set(tail)])
    os.replace(staged, QUEUE_DIR / name)         # only now does the worker see the new batch
    return len(tail)


def unqueue_status(keys):
    """Show files that left the queue as offered (or skipped) again."""
    with closing(connect()) as con, con:
        skipped = {k for (k,) in con.execute("SELECT key FROM skipped")}
        con.executemany("UPDATE files SET status = ? WHERE key = ? AND status = 'queued'",
                        [("skipped" if k in skipped else "offer", k) for k in keys])


STOP_MODES = ("skip", "all")


def worker_can_stop():
    """True if the running worker handles stop requests (one started by an older
    encode.sh doesn't, until its run is over)."""
    return (WORKER_LOCK / "stoppable").exists()


def request_stop(mode, key):
    """Ask the worker to stop the encode of key: "skip" continues with the next file,
    "all" also pauses the rest of the run (see requeue_rest). encode.sh checks the
    request every few seconds. Returns None, or why nothing was stopped."""
    cur = current_progress()
    if mode not in STOP_MODES or not cur or cur["key"] != key:
        return "that file isn't being encoded any more."
    if not worker_can_stop():
        return ("the running worker was started by an older version, which can't stop an encode. "
                "Stopping works from its next run.")
    write_text_atomic(STOP_FILE, f"{mode}\t{key}\n")
    unqueue_status([key])
    return None


def stop_requested():
    """(mode, key) of a stop request the worker hasn't carried out yet, or None."""
    try:
        mode, _, key = STOP_FILE.read_text(encoding="utf-8").strip().partition("\t")
    except OSError:
        return None
    return mode, key


def requeue_rest(done, pause=True):
    """After "Stop all", or when the library went offline (called by encode.sh, no
    database): put the entries of the current run after the first `done` back into the
    queue, ahead of other batches, finish the run's batches and, with `pause`, pause the
    queue. Returns how many were requeued."""
    cancelled = cancelled_entries()
    entries = run_entries()                     # the whole run, also batches already in done/
    if entries is None:
        entries = [e for e in batch_entries() if e[0].parent == PROC_DIR]
    rest = [(k, p) for f, k, p in entries[done:] if (f.name, k) not in cancelled]
    if pause:
        PAUSE_FILE.touch()                      # first, so the new batch doesn't start a worker
    if rest:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        name = f"batch-0-resumed-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.txt"   # sorts first
        tmp = DATA_DIR / f".{name}.tmp"
        tmp.write_text("".join(f"{k}\t{p}\n" if p else f"{k}\n" for k, p in rest), encoding="utf-8")
        os.replace(tmp, QUEUE_DIR / name)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    for f in dict.fromkeys(e[0] for e in entries):
        if f.parent == PROC_DIR and f.exists():
            os.replace(f, DONE_DIR / f.name)
    return len(rest)


def is_paused():
    return PAUSE_FILE.exists()


def resume_queue():
    PAUSE_FILE.unlink(missing_ok=True)


def status_text():
    q = pending_keys()
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
        lines.append(f"{len(q)} file(s) waiting or encoding.")
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


def write_batch(keys, preset=None):
    """Queue the given keys as a new batch, optionally with a preset id that overrides
    the share's preset. Keys already queued are left out. Returns the keys queued."""
    if preset and not presets.exists(preset):
        raise SystemExit(f"Unknown preset: {preset}")
    already = pending_keys()
    chosen = [k for k in dict.fromkeys(keys) if k not in already]
    if not chosen:
        return []
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    batch = QUEUE_DIR / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.txt"
    tmp = DATA_DIR / f".{batch.name}.tmp"         # same disk as queue/, so os.replace is atomic
    tmp.write_text("".join(f"{k}\t{preset}\n" if preset else f"{k}\n" for k in chosen), encoding="utf-8")
    os.replace(tmp, batch)                       # only now does the worker see the batch
    with closing(connect()) as con, con:         # show them as queued before the next scan
        con.executemany("UPDATE files SET status = 'queued' WHERE key = ?", [(k,) for k in chosen])
    return chosen


def cmd_queue(state, args):
    skip_rest = "--skip-rest" in args
    preset = None
    if "--preset" in args:
        i = args.index("--preset")
        if i + 1 >= len(args):
            raise SystemExit("--preset needs a preset id (see the presets/ folder or the web UI).")
        preset = args[i + 1]
        args = args[:i] + args[i + 2:]
    args = [a for a in args if a != "--skip-rest"]
    offer = state["last_offer"]
    if not offer:
        raise SystemExit("The last list is empty. Run 'scan' first.")
    nums = parse_selection(args, len(offer))
    chosen = write_batch([offer[i - 1] for i in nums], preset)
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
        print(f"Handlebreaks v{__version__}")
        return
    if cmd == "preset-check":                   # compatibility of the editor and presets with a HandBrakeCLI
        caps = presets.worker_capabilities() if "--worker" in rest else presets.capabilities()
        if not caps or not caps.get("available"):
            raise SystemExit("No HandBrakeCLI here" + (" and no published worker file." if "--worker" in rest else "."))
        print("\n".join(presets.audit(caps)))
        return
    if cmd == "handbrake-caps":                 # encode.sh: publish what this worker's HandBrakeCLI supports
        if presets.publish_worker_capabilities():
            print(f"{now_str()} HandBrakeCLI capabilities published to {presets.WORKER_FILE.name}")
        return
    apply_config(load_config())
    if cmd == "replace-jobs":                   # file moves only, no database (see run_replace_jobs)
        run_replace_jobs()
        return
    if cmd == "purge-originals":                # delete kept originals older than N days (default KEEP_ORIGINALS_DAYS)
        n, freed, held = purge_kept(int(rest[0]) if rest and rest[0].isdigit() else None)
        print(f"{n} kept original(s) deleted, {freed / 2**30:.2f} GiB freed."
              + (f" {held} kept because the file that replaced them is missing or its share can't be reached." if held else ""))
        return
    if cmd == "stop-rest":                      # encode.sh after "Stop all"; no database either
        print(requeue_rest(int(rest[0]), pause="--no-pause" not in rest))
        return
    for d in (QUEUE_DIR, PROC_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    state = load_state()

    if cmd == "scan":
        only = None
        if "--share" in rest:
            i = rest.index("--share")
            n = rest[i + 1] if i + 1 < len(rest) else ""
            if f"share{n}" not in ROOT_CANDIDATES:
                raise SystemExit(f"Unknown share: {n!r}. Shares: " +
                                 ", ".join(f"{p[5:]} ({SHARE_NAMES[p]})" for p in ROOT_CANDIDATES))
            only = [f"share{n}"]
        s = scan(state, probe_files="--no-probe" not in rest, only=only, force="--force" in rest)
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
