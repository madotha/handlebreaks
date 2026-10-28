#!/usr/bin/env python3
"""
Handlebreaks web UI
===================

Pages
-----
Files       All scanned videos as a folder tree with codec, resolution, bitrate,
            size and status. Filters by text, codec and status. Files can be
            selected and queued for encoding.
Status      Current encode, queue, latest results, start a scan.
Stats       Distribution of codecs, bitrates, file sizes, resolutions and statuses.

With SCAN_INTERVAL (minutes) set in config.env, the web UI also rescans all shares
automatically once the last scan is that old (see auto_scan_loop).
Settings    The values from config.env, editable in separate sections.

There is no login yet. The web UI therefore listens on 127.0.0.1 by default.
In Docker, docker-compose.yml publishes the port on 127.0.0.1 only.

Start
-----
    python3 webui.py                          # http://127.0.0.1:7979
    python3 webui.py --host 0.0.0.0 --port 7979
Host and port can also be set with WEB_HOST and WEB_PORT.
"""

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from contextlib import closing
from pathlib import Path

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

import encoding as enc
import plex
import presets

app = Flask(__name__)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  MAX_CONTENT_LENGTH=5 * 1024 * 1024)      # preset uploads


def secret_key():
    """Random key for session cookies, created once per installation."""
    f = enc.DATA_DIR / ".secret_key"
    if not f.exists():
        enc.DATA_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


app.secret_key = secret_key()

# ─── Settings ─────────────────────────────────────────────────────────────────

# Notification groups (NOTIFY_EVENTS): value -> label in the settings
EVENT_GROUPS = {
    "encodes": "Encodes finished or failed",
    "runs": "Worker runs finished or interrupted (runs of two or more files)",
    "replaces": "Replaces done or failed",
    "scans": "Scans finished or failed (automatic scans only when they find new files)",
    "shares": "Shares or the output folder going offline and coming back",
    "cleanup": "Kept originals deleted automatically",
}

# section -> (title, [(key, label, type, hint)])
SETTINGS = {
    "library": ("Library", []),                 # fields built from config.env (library_fields)
    "encoding": ("Encoding", [
        ("DEFAULT_PRESET", "Default preset", "preset",
         "Used for every share without its own preset. Manage presets on the Presets page."),
    ]),
    "notes": ("Obsidian", [
        ("NOTE_DIR", "Notes folder", "paths", "For \"Encoding To-Do.md\" and \"Encoding Status.md\". Empty = no notes."),
        ("STATUS_NOTE_ENABLED", "Update the status note every minute while encoding", "bool", ""),
    ]),
    "display": ("Display", [
        ("DATE_FORMAT", "Date format", "date_format",
         "How dates are shown everywhere in the web UI, always followed by the time (24 h). Default YYYY-MM-DD."),
    ]),
    "notifications": ("Notifications", [
        ("NOTIFY_SECONDS", "Hide notifications after (seconds)", "number",
         "Notifications pop up in the corner of every page of the web UI that is open. "
         "Hovering one keeps it open. 0 = keep them until you close them. Default 8."),
        ("NOTIFY_ERRORS_STAY", "Keep errors until closed", "bool",
         "Failed encodes, replaces and scans stay until you close them, whatever the time above. On by default."),
        ("NOTIFY_EVENTS", "Notify about", "event_groups",
         "Your own actions (queueing, removing, deleting) aren't notified: the page confirms them right away."),
    ]),
    "status": ("Status", [
        ("HISTORY_KEEP_DAYS", "Keep history entries visible (days)", "number",
         "The Status page's History hides results and replaces older than this. Nothing is deleted. 0 = always show. Default 30."),
    ]),
    "scans": ("Scans", [
        ("SCAN_INTERVAL", "Automatic rescan interval (minutes)", "number",
         "Rescans all shares when the last scan is this many minutes old; only new or changed "
         "files are read with ffprobe. 0 = off. Runs while the web UI runs (always in Docker)."),
        ("FFPROBE_WORKERS", "Parallel ffprobe calls", "number",
         f"How many files are read at the same time during scans. Empty or 0 = number of CPUs "
         f"({os.cpu_count()} here). Lower it if the NAS gets slow during scans."),
    ]),
    "replace": ("Replace", [
        ("RENAME_CODEC_TAGS", "Codec tags in file names", "text",
         "\"Replace…\" suggests the original name with its codec tag (h264, x264, H.264, AVC, XviD …) "
         "replaced by the encode's codec. Format: codec=tag pairs, e.g. \"hevc=x265 av1=AV1\". "
         "Empty = hevc=x265 av1=AV1 vvc=VVC h264=x264."),
        ("RENAME_QUALITY", "Add the quality after the release group (e.g. x265-donm.rf20 or x265-donm.avg2000)", "bool",
         "From the preset the file was encoded with: rf<RF value> for constant quality, avg<kbit/s> for average bitrate."),
        ("KEEP_ORIGINALS", "Keep originals when replacing", "bool",
         "Default of the \"Keep the original\" option in the Replace dialogs (each replace can still change it). "
         "On: the original goes to the output folder's .replaced folder. Off: it is deleted right after the move. On by default."),
        ("KEEP_ORIGINALS_DAYS", "Delete kept originals after (days)", "number",
         "\"Replace…\" keeps the original in the output folder's .replaced folder (see Status → Kept originals). "
         "With a number here they are deleted permanently once they were kept that long, "
         "checked hourly while the web UI runs. 0 = never (default)."),
        ("RENAME_GROUP", "Your release group", "text",
         "Optional. Replaces the release group after the codec tag, e.g. \"donm\": "
         "\"…h264-NTB.mkv\" becomes \"…x265-donm.mkv\". Empty = keep the original group."),
    ]),
    "plex": ("Plex", [
        ("PLEX_URL", "Plex server URL", "text",
         "E.g. http://127.0.0.1:32400. Used after \"Replace…\" to make Plex analyze the item or scan its "
         "folder. The token (PLEX_TOKEN) is set in config.env only, never shown here. Empty = off."),
    ]),
    "worker": ("Worker", [
        ("WORKER_ENABLED", "Encode inside the Docker container", "bool",
         "Docker only. Turn off when the Mac encodes natively with launchd and encode.sh, "
         "and the container only runs the web UI and scans."),
    ]),
}

# Defaults the code uses when a key is missing from an older config.env: shown on the form, so
# saving a section doesn't turn them into 0 / off
# DATE_FORMAT values: the label is the stored value, the strftime pattern renders the server's
# fallback text (static/time.js builds the same order from html[data-date-format])
DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d", "DD-MM-YYYY": "%d-%m-%Y", "MM-DD-YYYY": "%m-%d-%Y",
    "DD.MM.YYYY": "%d.%m.%Y", "DD/MM/YYYY": "%d/%m/%Y", "MM/DD/YYYY": "%m/%d/%Y",
}
DEFAULT_DATE_FORMAT = "YYYY-MM-DD"


def date_format():
    """The configured DATE_FORMAT, or the default if it's missing or unknown."""
    value = (enc.CONFIG.get("DATE_FORMAT") or "").strip().upper()   # loaded for each request
    return value if value in DATE_FORMATS else DEFAULT_DATE_FORMAT


SETTING_DEFAULTS = {"DATE_FORMAT": DEFAULT_DATE_FORMAT, "HISTORY_KEEP_DAYS": "30", "RENAME_QUALITY": "1", "KEEP_ORIGINALS": "1",
                    "KEEP_ORIGINALS_DAYS": "0", "NOTIFY_SECONDS": "8", "NOTIFY_ERRORS_STAY": "1",
                    "NOTIFY_EVENTS": " ".join(EVENT_GROUPS)}

# Settings overridden by an environment variable of the same name (docker-entrypoint.sh)
ENV_OVERRIDES = {"WORKER_ENABLED"}

# Allowed: everything except characters that could run code in config.env (read by zsh)
# or end the quotes. $ only as $HOME or ${HOME}.
FORBIDDEN_RE = re.compile(r'["`\\\n\r]|\$(?!HOME\b|\{HOME\})')


def raw_config():
    """Values as written in config.env ($HOME not expanded), otherwise from the template."""
    src = enc.CONFIG_FILE if enc.CONFIG_FILE.exists() else enc.EXAMPLE_CONFIG
    return enc.parse_config(src, expand=False)


def library_fields(cfg):
    """Settings fields of the library section below the share list (shares have their own
    forms, see share_list() and share_save())."""
    return [("ENCODED_DIR", "Output folder", "paths", "Finished files land here and wait for your review.")]


def share_list(cfg):
    """The shares for the Library settings: config values as written, path checks, preset
    name, number of files and last scan."""
    info = share_info()
    names = dict(preset_choices())
    out = []
    for sh in enc.shares(cfg):
        n = sh["n"]
        preset = cfg.get(f"SHARE_{n}_PRESET", "")
        out.append(dict(sh, folder=cfg.get(f"SHARE_{n}", ""), raw_name=cfg.get(f"SHARE_{n}_NAME", ""),
                        checks=path_checks(cfg.get(f"SHARE_{n}", "")), preset_id=preset,
                        preset_name=names.get(preset, preset) if preset else None,
                        **info.get(sh["prefix"], {"files": 0, "scanned": None})))
    return out


@app.route("/settings/shares/<n>", methods=["POST"])
def share_save(n):
    """Add a share (n = "new", gets the next free number) or change one."""
    cfg = raw_config()
    if n != "new" and (not n.isdigit() or not cfg.get(f"SHARE_{n}")):
        abort(404)
    folder, name, preset = (request.form.get(k, "").strip() for k in ("folder", "name", "preset"))
    problem = ("Enter the share's folder. To remove a share, use its delete button." if not folder else
               'Not allowed: " ` \\ line breaks and $ (except $HOME).' if any(FORBIDDEN_RE.search(v) for v in (folder, name)) else
               "Unknown preset." if preset and preset not in dict(preset_choices()) else None)
    if problem:
        flash(problem, "error")
        return redirect(url_for("settings", section="library", open=f"share{n}" if n != "new" else "new"))
    num = enc.next_share_number() if n == "new" else int(n)
    name = name or Path(enc.path_list(os.path.expandvars(folder))[0]).name or f"Share {num}"
    enc.write_config({f"SHARE_{num}": folder, f"SHARE_{num}_NAME": name, f"SHARE_{num}_PRESET": preset})
    flash(f"Share \"{name}\" {'added' if n == 'new' else 'saved'}."
          + (" Start its initial scan with the scan button." if n == "new" else ""), "ok")
    return redirect(url_for("settings", section="library"))


@app.route("/settings/shares/<int:n>/delete", methods=["POST"])
def share_delete(n):
    """Remove a share: its settings are commented out, so its number stays reserved."""
    cfg = raw_config()
    current = enc.shares(cfg)
    share = next((s for s in current if s["n"] == n), None)
    if not share:
        abort(404)
    if len(current) == 1:
        flash("At least one share is needed. Add another one before removing this one.", "error")
    else:
        enc.write_config({f"SHARE_{n}": None, f"SHARE_{n}_NAME": None, f"SHARE_{n}_PRESET": None})
        flash(f"Removed {share['name']}. Its files disappear from the list with the next scan.", "ok")
    return redirect(url_for("settings", section="library"))


def path_checks(value):
    """For each path of a ":" list: (path, exists)."""
    return [(p, os.path.exists(p)) for p in enc.path_list(os.path.expandvars(value))]


def share_info():
    """Key prefix -> {"files": number of files in the inventory, "scanned": last scan time}."""
    with closing(db()) as con:
        counts = dict(con.execute("SELECT substr(key, 1, instr(key, '/') - 1), COUNT(*) FROM files GROUP BY 1"))
    scans = enc.share_scans()
    return {p: {"files": counts.get(p, 0), "scanned": scans.get(p)} for p in enc.ROOT_CANDIDATES}


def plex_status(cfg):
    """(ok, message) of a connection test with PLEX_URL and PLEX_TOKEN (the token is never shown)."""
    full = enc.parse_config(enc.CONFIG_FILE) if enc.CONFIG_FILE.exists() else {}
    if not full.get("PLEX_URL"):
        return None
    if not full.get("PLEX_TOKEN"):
        return False, "PLEX_TOKEN isn't set in config.env."
    try:
        sections = plex.Plex(full["PLEX_URL"], full["PLEX_TOKEN"], timeout=5).sections()
    except plex.PlexError as e:
        return False, str(e)
    shared = [t for _, t, locs in sections if any(any(str(p).startswith(l + "/") or str(p) == l
              for p in paths for l in locs) for paths in enc.ROOT_CANDIDATES.values())]
    return True, (f"Connected; token accepted. {len(sections)} libraries"
                  + (f", matching your shares: {', '.join(shared)}." if shared else "; none matches a share path."))


def preset_choices():
    """[(id, name)] of all presets, for dropdowns."""
    return [(pid, p.get("PresetName", pid)) for pid, p in presets.all_presets()]


# ─── Background scan ──────────────────────────────────────────────────────────

SCAN = {"running": False, "started": None, "finished": None, "error": None, "summary": None, "label": None}
SCAN_GUARD = threading.Lock()


def start_background_scan(label, only=None, force=False, path=None, auto=False):
    """Start a scan in a background thread. Returns the thread, or None if a scan is
    already running. auto: started by auto_scan_loop (notifies only about new files)."""
    with SCAN_GUARD:
        if SCAN["running"]:
            return None
        SCAN.update(running=True, started=enc.now_str(), finished=None, error=None, summary=None, label=label,
                    auto=auto)
        thread = threading.Thread(target=run_scan, args=(only, force, path), daemon=True)
        thread.start()
        return thread


def scan_interval(cfg):
    """SCAN_INTERVAL in minutes; 0 (off) if missing or invalid."""
    try:
        return max(int(cfg.get("SCAN_INTERVAL") or 0), 0)
    except ValueError:
        return 0


def next_auto_scan(cfg):
    """When the next automatic scan is due (datetime), or None if it's off."""
    minutes = scan_interval(cfg)
    if not minutes:
        return None
    last = enc.last_scan()
    try:
        return datetime.strptime(last, "%Y-%m-%d %H:%M") + timedelta(minutes=minutes)
    except (TypeError, ValueError):             # never scanned: due now
        return datetime.now()


def auto_scan_tick():
    """One check of the automatic rescan: start a scan of all shares if one is due."""
    cfg = enc.load_config()
    enc.apply_config(cfg)
    due = next_auto_scan(cfg)
    if due and datetime.now() >= due and not offline_now():   # waits until every share is back
        start_background_scan(f"Automatic scan of all shares (every {scan_interval(cfg)} min)", auto=True)


def auto_scan_loop():
    """Checks once a minute whether an automatic scan is due (SCAN_INTERVAL)."""
    while True:
        time.sleep(60)
        try:
            auto_scan_tick()
        except SystemExit as e:                 # e.g. config.env incomplete: try again later
            print(f"Automatic scan skipped: {e}", file=sys.stderr)
        except Exception as e:                  # keep the loop alive
            print(f"Automatic scan failed: {type(e).__name__}: {e}", file=sys.stderr)


def originals_purge_loop():
    """Deletes kept originals older than KEEP_ORIGINALS_DAYS: two minutes after the start, then hourly."""
    time.sleep(120)
    while True:
        try:
            enc.apply_config(enc.load_config())
            count, freed = enc.purge_kept()
            if count:
                _KEPT_GEN[0] += 1
                notify("purge", None, f"{count:,} kept original{'s' if count != 1 else ''} older than "
                                      f"{enc.keep_originals_days()} days, {fmt_size(freed)} freed.")
                print(f"{enc.now_str()} {count} kept original(s) older than {enc.keep_originals_days()} days "
                      f"deleted, {fmt_size(freed)} freed.", file=sys.stderr)
        except SystemExit as e:                 # e.g. config.env incomplete: try again later
            print(f"Deleting old originals skipped: {e}", file=sys.stderr)
        except Exception as e:                  # keep the loop alive
            print(f"Deleting old originals failed: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(3600)


REPLACE_SYNC = threading.Lock()


def sync_replaces():
    """Bring finished replace jobs into the database: rescan the folder of each (new or
    changed files only), then mark the job as synced. Runs in the background and when a
    page asks for a job's result, so it happens even if nobody waits on the page.
    Returns False if a scan was running and blocked it (tried again later)."""
    with REPLACE_SYNC:
        jobs = enc.unsynced_replaces()
        if not jobs:
            return True
        enc.apply_config(enc.load_config())
        folders = {}
        for f, job in jobs:
            folders.setdefault(job["key"].rsplit("/", 1)[0], []).append((f, job))
        for folder, items in folders.items():
            done = [j for _, j in items if j.get("state") == "done"]
            if done:
                with closing(db()) as con:      # the new file keeps the original's "date added"
                    added = {j["key"]: con.execute("SELECT added FROM files WHERE key = ?", (j["key"],)).fetchone()
                             for j in done}
                try:
                    enc.scan(enc.load_state(), path=folder)
                except SystemExit as e:
                    if "already running" in str(e):
                        return False
                    print(f"Replace sync of {folder}: {e}", file=sys.stderr)   # e.g. share offline
                with closing(db()) as con, con:
                    con.executemany("UPDATE files SET added = ? WHERE key = ?",
                                    [(added[j["key"]][0], j["new_key"]) for j in done
                                     if added.get(j["key"]) and added[j["key"]][0] and j.get("new_key")])
            for f, job in items:
                enc.mark_replace_synced(f, job)
            enc.add_events([("replace_done", job.get("new_key") or job["key"], None, None)
                            if job.get("state") == "done" else
                            ("replace_failed", job["key"], job.get("message"), None) for _, job in items])
        return True


def replace_sync_loop():
    """Every 10 s: put the results of finished replace jobs into the database."""
    while True:
        time.sleep(10)
        try:
            sync_replaces()
        except Exception as e:                  # keep the loop alive
            print(f"Replace sync failed: {type(e).__name__}: {e}", file=sys.stderr)


# ─── Notifications ────────────────────────────────────────────────────────────
# event_loop() and the places where things happen store events (enc.add_events); every page
# polls /events (static/notify.js) and shows the new ones as popups.

# kind -> (group in NOTIFY_EVENTS, level, title, link)
EVENT_KINDS = {
    "encode_ok": ("encodes", "success", "Encode finished", "review"),
    "encode_fail": ("encodes", "error", "Encode failed", "history"),
    "run_done": ("runs", "success", "Run finished", "review"),
    "run_interrupted": ("runs", "warn", "Run interrupted", "queue"),
    "replace_done": ("replaces", "success", "Replaced", "history"),
    "replace_failed": ("replaces", "error", "Replace failed", "review"),
    "scan_new": ("scans", "success", "New files found", "files_new"),
    "scan_done": ("scans", "success", "Scan finished", "files"),
    "scan_failed": ("scans", "error", "Scan failed", "library"),
    "offline": ("shares", "warn", "Not reachable", "library"),
    "online": ("shares", "success", "Reachable again", "library"),
    "purge": ("cleanup", "info", "Old originals deleted", "originals"),
}
# Several events of these kinds in one poll become one notification
EVENT_PLURALS = {"encode_ok": "{n} encodes finished", "encode_fail": "{n} encodes failed",
                 "replace_done": "{n} files replaced", "replace_failed": "{n} replaces failed"}
LEVEL_ORDER = ("info", "success", "warn", "error")
EVENTS_SHOWN_HOURS = 24                         # a page opened later doesn't show older ones


def notify(kind, key=None, detail=None, level=None):
    enc.add_events([(kind, key, detail, level)])


def notify_groups():
    """The groups of NOTIFY_EVENTS (all if it's missing, none for "none")."""
    value = enc.CONFIG.get("NOTIFY_EVENTS")
    return set(EVENT_GROUPS) if value is None else set(value.split()) & set(EVENT_GROUPS)


def notify_seconds():
    try:
        return min(max(int(enc.CONFIG.get("NOTIFY_SECONDS") or 8), 0), 3600)
    except ValueError:
        return 8


_WATCH = {"ready": False, "run": None, "run_start": None, "offline": set()}


def run_events():
    """Events for a worker run that ended: run.txt disappeared or was rewritten by the next run."""
    try:
        stamp = enc.RUN_FILE.stat().st_mtime_ns
    except OSError:
        stamp = None
    prev, start = _WATCH["run"], _WATCH["run_start"]
    if stamp != prev:
        _WATCH.update(run=stamp, run_start=enc.run_started() if stamp else None)
    if prev is None or stamp == prev or not start:
        return []
    if enc.is_paused():                         # "Stop all" on the Status page: the user knows
        return []
    with closing(db()) as con:
        counts = Counter(dict(con.execute("SELECT status, COUNT(*) FROM results WHERE at >= ? GROUP BY status",
                                          (start,))))
    parts = [f"{counts[s]:,} {label}" for s, label in (("OK", "encoded"), ("FAIL", "failed"),
             ("STOPPED", "stopped"), ("MISSING", "missing"), ("EXISTS", "already encoded")) if counts[s]]
    stamp_start = datetime.strptime(start, "%Y-%m-%d %H:%M").timestamp()
    requeued = sum(len(enc.read_batch(f)) for f in enc.QUEUE_DIR.glob("batch-0-resumed-*.txt")
                   if f.stat().st_mtime >= stamp_start) if enc.QUEUE_DIR.exists() else 0
    if requeued:                                # encode.sh went_offline(): the rest is back in the queue
        return [("run_interrupted", None, f"A share or the output folder went offline. {requeued:,} "
                 f"file{'s' if requeued != 1 else ''} back in the queue; encoding goes on once it's back."
                 + (f" Done before: {', '.join(parts)}." if parts else ""), None)]
    if sum(counts.values()) < 2:                # a single file has its own notification
        return []
    return [("run_done", None, ", ".join(parts) + ".", "warn" if counts["FAIL"] else None)]


def event_tick():
    """One round of event_loop()."""
    rows = [("encode_ok" if status == "OK" else "encode_fail", key, None, None)
            for _, status, key in enc.new_results() if status in ("OK", "FAIL")]
    rows += run_events()
    offline = set(offline_now())
    if _WATCH["ready"]:                         # the first round only learns the current state
        rows += [("offline", None, n, None) for n in sorted(offline - _WATCH["offline"])]
        rows += [("online", None, n, None) for n in sorted(_WATCH["offline"] - offline)]
    _WATCH.update(offline=offline, ready=True)
    enc.add_events(rows)


def event_loop():
    """Every 5 s: turn what the worker did (results.tsv, run.txt) and changes in which shares
    are reachable into notifications; once an hour, delete old ones."""
    pruned = 0.0
    while True:
        try:
            enc.apply_config(enc.load_config())
            event_tick()
            if time.monotonic() - pruned > 3600:
                enc.prune_events()
                pruned = time.monotonic()
        except SystemExit as e:                 # e.g. config.env incomplete: try again later
            print(f"Notifications skipped: {e}", file=sys.stderr)
        except Exception as e:                  # keep the loop alive
            print(f"Notifications failed: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(5)


def event_url(link):
    if link == "files_new":
        return url_for("files", added="24h")
    if link == "files":
        return url_for("files")
    if link == "library":
        return url_for("settings", section="library")
    return url_for("status", view=link)


@app.route("/events")
def events():
    """New notifications for static/notify.js: after = the last id the page has seen (none =
    only the newest id, so a fresh page starts now). Several encodes or replaces of one poll
    are merged, groups switched off in NOTIFY_EVENTS are left out."""
    after = request.args.get("after", "")
    since = (datetime.now() - timedelta(hours=EVENTS_SHOWN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    last, rows = enc.events_after(int(after) if after.isdigit() else None, since)
    groups = notify_groups()
    by_kind = {}
    for ev in rows:
        spec = EVENT_KINDS.get(ev["kind"])
        if spec and spec[0] in groups:
            by_kind.setdefault((ev["kind"], ev["id"] if ev["kind"] not in EVENT_PLURALS else 0), []).append(ev)
    items = []
    for (kind, _), evs in by_kind.items():
        _, level, title, link = EVENT_KINDS[kind]
        level = max((e["level"] or level for e in evs), key=LEVEL_ORDER.index)
        at = datetime.strptime(evs[-1]["at"], "%Y-%m-%d %H:%M:%S").astimezone().isoformat(timespec="seconds")
        item = {"id": evs[-1]["id"], "kind": kind, "level": level, "title": title, "url": event_url(link),
                "at": at, "where": "", "file": "", "text": evs[0]["detail"] or ""}
        if len(evs) > 1:
            names = [entry_parts(e["key"])[1] for e in evs if e["key"]]
            item["title"] = EVENT_PLURALS[kind].format(n=len(evs))
            item["text"] = ", ".join(names[:2]) + (f" and {len(names) - 2:,} more" if len(names) > 2 else "")
        elif evs[0]["key"]:
            item["where"], item["file"] = entry_parts(evs[0]["key"])
        items.append(item)
    items.sort(key=lambda i: i["id"])
    return {"last": last, "seconds": notify_seconds(),
            "errors_stay": (enc.CONFIG.get("NOTIFY_ERRORS_STAY") or "1").strip() != "0", "events": items}


def path_summary(path):
    """'3 files: 2 optimized, 1 offered' for the files of one file or folder."""
    with closing(db()) as con:
        counts = Counter(dict(con.execute(
            "SELECT status, COUNT(*) FROM files WHERE key = ? OR (key >= ? AND key < ?) GROUP BY status",
            (path, path + "/", path + "0"))))
    total = sum(counts.values())
    if not total:
        return "no video files found (removed from the list if they were there before)"
    parts = [f"{n:,} {enc.STATUS_LABELS.get(st, st).lower()}" for st, n in counts.most_common()]
    return f"{total:,} file{'s' if total != 1 else ''}: " + ", ".join(parts)


def run_scan(only=None, force=False, path=None):
    try:
        enc.apply_config(enc.load_config())
        s = enc.scan(enc.load_state(), only=only, force=force, path=path)
        if path:
            SCAN.update(error=None, summary=f"{SCAN['label']}: {path_summary(path)}")
        else:
            SCAN.update(error=None, summary=f"{SCAN['label']}: {s['videos_total']:,} videos in the library, "
                                            f"{s['already_h265']:,} already optimized, {len(s['offer']):,} offered")
            new = sum(1 for o in s["offer"] if o["hint"] == "🆕")
            if new:
                notify("scan_new", None, f"{new:,} new file{'s' if new != 1 else ''} offered for encoding.")
            elif not SCAN.get("auto"):
                offered = len(s["offer"])
                notify("scan_done", None, f"{offered:,} file{'s' if offered != 1 else ''} offered, none of them new."
                       if offered else "Nothing new to encode.")
    except SystemExit as e:
        SCAN.update(error=str(e), summary=None)
    except Exception as e:                      # show errors in the UI instead of swallowing them
        SCAN.update(error=f"{type(e).__name__}: {e}", summary=None)
    finally:
        if SCAN["error"] and not path:
            notify("scan_failed", None, SCAN["error"])
        SCAN.update(running=False, finished=enc.now_str())

# ─── Requests ─────────────────────────────────────────────────────────────────


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals.update(csrf_token=csrf_token, settings_sections=SETTINGS, scan_state=SCAN, now=datetime.now,
                             app_version=enc.__version__)


@app.before_request
def before():
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Invalid form token. Reload the page and try again.")
    g.config_error = None
    try:
        enc.apply_config(enc.load_config())
    except SystemExit as e:
        g.config_error = str(e)


@app.after_request
def compress(resp):
    """gzip larger HTML/JSON responses (a big folder level is ~1 MB of repetitive HTML)."""
    if (resp.direct_passthrough or not 200 <= resp.status_code < 300 or resp.headers.get("Content-Encoding")
            or "gzip" not in request.headers.get("Accept-Encoding", "")
            or resp.mimetype not in ("text/html", "application/json")):
        return resp
    data = resp.get_data()
    if len(data) < 2048:
        return resp
    resp.set_data(gzip.compress(data, compresslevel=5))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


_OFFLINE = [0.0, []]                            # [checked at, names] for offline_now()


def offline_now():
    """Shares and output folder that aren't reachable (enc.offline_shares()), checked at most
    every 30 s so pages don't touch every share on each request."""
    if time.monotonic() - _OFFLINE[0] > 30:
        _OFFLINE[:] = [time.monotonic(), enc.offline_shares()]
    return _OFFLINE[1]


@app.context_processor
def inject():
    offline = [] if g.get("config_error") else offline_now()
    wait = None if g.get("config_error") else enc.worker_wait()
    if wait and wait["kind"] == "offline" and wait["name"] in offline:
        wait = None                             # the "Not reachable" banner already says it
    return {"config_error": g.get("config_error"), "offline": offline, "worker_wait": wait,
            "date_format": date_format(),
            "keep_originals": (enc.CONFIG.get("KEEP_ORIGINALS") or "1").strip() != "0",
            "in_docker": Path("/.dockerenv").exists()}


# Display names for ffprobe codec names; anything else is shown in upper case
CODEC_NAMES = {
    "hevc": "H.265 (HEVC)", "av1": "AV1", "vvc": "H.266 (VVC)", "h264": "H.264 (AVC)",
    "mpeg4": "MPEG-4 Part 2", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1", "vc1": "VC-1",
    "vp9": "VP9", "vp8": "VP8", "wmv3": "WMV 9", "wmv2": "WMV 8", "msmpeg4v3": "MS MPEG-4 v3",
    "mjpeg": "Motion JPEG",
}
UNKNOWN_CODEC = "unknown"                       # filter value for files without probe data


@app.template_filter("codec_name")
def codec_name(c):
    if not c or c == UNKNOWN_CODEC:
        return "Not probed"
    return CODEC_NAMES.get(c, c.upper())


@app.template_global()
def codec_class(c):
    """good = optimized codec (green), other = everything else (orange), unknown = not probed."""
    if not c or c == UNKNOWN_CODEC:
        return "unknown"
    return "good" if c in enc.OPTIMIZED_CODECS else "other"


def to_datetime(value):
    """Stored time ("YYYY-MM-DD HH:MM", in the server's local time) or datetime -> aware datetime."""
    if isinstance(value, datetime):
        return value.astimezone()
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").astimezone()
    except (TypeError, ValueError):
        return None


@app.template_filter("localtime")
def localtime(value):
    """A <time> element with the UTC offset; static/time.js shows it in the viewer's time zone
    (the server, e.g. a Docker container, may run in another zone such as UTC)."""
    dt = to_datetime(value)
    if dt is None:
        return value or "–"
    return Markup('<time datetime="{}">{}</time>').format(dt.isoformat(timespec="minutes"),
                                                          dt.strftime(DATE_FORMATS[date_format()] + " %H:%M"))


@app.template_filter("added")
def added_time(epoch):
    """Date added (epoch seconds) as a <time>, like the localtime filter."""
    if not epoch:
        return "–"
    dt = datetime.fromtimestamp(epoch).astimezone()
    return Markup('<time datetime="{}">{}</time>').format(
        dt.isoformat(timespec="minutes"), dt.strftime(DATE_FORMATS[date_format()] + " %H:%M"))


@app.template_filter("wbr")
def word_breaks(name):
    """A file name with line-break opportunities after . _ - and before ›, so long release
    names wrap at their separators instead of in the middle of a word."""
    text = str(escape(name or ""))
    return Markup(re.sub(r"([._\-]|&gt;|›)(?=\S)", r"\1<wbr>", text))


@app.template_filter("count")
def fmt_count(n):
    return f"{n:,}"


@app.template_filter("size")
def fmt_size(n):
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


@app.template_filter("bitrate")
def fmt_bitrate(b):
    return f"{b / 1e6:.1f} Mbit/s" if b else "–"


@app.template_filter("duration")
def fmt_duration(s):
    if not s:
        return "–"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d} h"


@app.route("/health")
def health():
    return "ok"


@app.route("/")
def index():
    return redirect(url_for("files"))


# ─── Files: folder tree, loaded one level at a time ───────────────────────────
# Large libraries have tens of thousands of files, so the page never renders them
# all: it shows the top folders, and each folder's content is fetched when opened.

PAGE_SIZE = 200                                 # files per "Show more" step
STARTED = time.time()                           # part of the folder cache version: a restart or update clears it
FOLDER_PAGE = 100                               # subfolders per "Show more folders" step


_CACHE = {}


def cached(name, version, compute):
    """compute() once per version (e.g. enc.db_version()); repeated page views are free."""
    hit = _CACHE.get(name)
    if hit and hit[0] == version:
        return hit[1]
    value = compute()
    _CACHE[name] = (version, value)
    return value


def natural(name):
    """Sort key that orders "Season 2" before "Season 10"."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


CODEC_GROUPS = {"group:good": "All optimized codecs", "group:other": "All other codecs"}


def codec_summary(selected):
    """Label of the codec filter button: "All codecs", one name, or "N codecs"."""
    if not selected:
        return "All codecs"
    if len(selected) == 1:
        return CODEC_GROUPS.get(selected[0]) or codec_name(selected[0])
    return f"{len(selected)} codecs"


ADDED_RANGES = {"24h": ("Last 24 hours", 1), "7d": ("Last 7 days", 7), "30d": ("Last 30 days", 30),
                "custom": ("Custom range", None)}


def parse_date(text):
    """yyyy-mm-dd (what date inputs send) or dd.mm.yyyy (older links) -> datetime
    (midnight, server time), or None."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            pass
    return None


def file_filters(src):
    """Filter values from a request (query string or form). codec is a list: codec
    names, "unknown" (not probed) and the groups in CODEC_GROUPS can be combined.
    added is one of ADDED_RANGES; "custom" uses from/to (yyyy-mm-dd, or dd.mm.yyyy from
    older links, either may be empty); they are kept as yyyy-mm-dd, invalid dates are
    left out (see added_problem)."""
    filters = {k: src.get(k, "").strip() for k in ("q", "status") if src.get(k, "").strip()}
    codecs = [c.strip() for c in src.getlist("codec") if c.strip()]
    if codecs:
        filters["codec"] = codecs
    added = src.get("added", "")
    if added in ADDED_RANGES:
        if added != "custom":
            filters["added"] = added
        else:
            dates = {k: parse_date(src.get(k, "")).strftime("%Y-%m-%d") for k in ("from", "to") if parse_date(src.get(k, ""))}
            if dates:
                filters.update(added="custom", **dates)
    return filters


def added_problem(src):
    """Message for an invalid custom date range, or None."""
    if src.get("added") != "custom":
        return None
    bad = [src.get(k, "").strip() for k in ("from", "to") if src.get(k, "").strip() and not parse_date(src.get(k, ""))]
    if bad:
        return f"Not a date: {', '.join(bad)}. Pick the dates with the date fields."
    if not any(src.get(k, "").strip() for k in ("from", "to")):
        return "Pick a start or end date for the custom range."
    lo, hi = parse_date(src.get("from", "")), parse_date(src.get("to", ""))
    if lo and hi and lo > hi:
        return "The start date is after the end date."
    return None


def added_bounds(filters):
    """(from, to) as epoch seconds for the date added filter (either may be None), or None."""
    added = filters.get("added")
    if added in ("24h", "7d", "30d"):
        return time.time() - ADDED_RANGES[added][1] * 86400, None
    if added == "custom":
        lo, hi = parse_date(filters.get("from", "")), parse_date(filters.get("to", ""))
        return (lo.timestamp() if lo else None,
                (hi + timedelta(days=1)).timestamp() if hi else None)   # "to" includes that whole day
    return None


def filter_sql(filters, path=""):
    """WHERE clause and parameters for the filters, limited to the folder `path`."""
    where, params = [], []
    bounds = added_bounds(filters)
    if bounds:
        for op, value in zip((">=", "<"), bounds):
            if value is not None:
                where.append(f"added {op} ?")
                params.append(value)
    if filters.get("q"):
        where.append("instr(pylower(key), ?) > 0")
        params.append(filters["q"].lower())
    if filters.get("codec"):                    # any of the selected codecs or groups
        good = sorted(enc.OPTIMIZED_CODECS)
        names = [c for c in filters["codec"] if c != UNKNOWN_CODEC and c not in CODEC_GROUPS]
        alts = []
        if names:
            alts.append(f"video_codec IN ({', '.join('?' * len(names))})")
            params += names
        if UNKNOWN_CODEC in filters["codec"]:
            alts.append("video_codec IS NULL")
        if "group:good" in filters["codec"]:
            alts.append(f"video_codec IN ({', '.join('?' * len(good))})")
            params += good
        if "group:other" in filters["codec"]:
            alts.append(f"(video_codec IS NOT NULL AND video_codec NOT IN ({', '.join('?' * len(good))}))")
            params += good
        where.append("(" + " OR ".join(alts) + ")")
    if filters.get("status"):
        where.append("status = ?")
        params.append(filters["status"])
    if path:                                    # all keys below path/ ("0" sorts right after "/")
        where.append("key >= ? AND key < ?")
        params += [path + "/", path + "0"]
    return (" WHERE " + " AND ".join(where)) if where else "", params


def db():
    con = enc.connect()
    con.create_function("pylower", 1, str.lower, deterministic=True)   # Unicode-aware, unlike lower()
    return con


def root_folders():
    """Key prefix -> (share name, folder name on disk), e.g. "share4" -> ("Anime", "anime-movies")."""
    out = {}
    for prefix, cands in enc.ROOT_CANDIDATES.items():
        root = enc.first_existing(cands)
        out[prefix] = (enc.SHARE_NAMES.get(prefix, prefix), (root or Path(cands[0])).name if cands else "")
    return out


# Sortable fields of the file list: field -> (label, sort key of a row). Rows without a
# value (e.g. not probed) always come last, in both directions.
SORT_FIELDS = {
    "name":       ("File", lambda r: natural(r["key"].rsplit("/", 1)[-1])),
    "codec":      ("Codec", lambda r: codec_name(r["video_codec"]).lower() if r["video_codec"] else None),
    "resolution": ("Resolution", lambda r: r["width"] * r["height"] if r["width"] and r["height"] else None),
    "bitrate":    ("Bitrate", lambda r: r["bitrate"]),
    "duration":   ("Duration", lambda r: r["duration"]),
    "size":       ("Size", lambda r: r["size"]),
    "added":      ("Added", lambda r: r["added"]),
    "status":     ("Status", lambda r: list(enc.STATUS_LABELS).index(r["status"])
                   if r["status"] in enc.STATUS_LABELS else len(enc.STATUS_LABELS)),
}
# How folders are ordered for a field: by size, by optimized share, by their newest file,
# otherwise by name
FOLDER_SORT = {"size": lambda f: f["size"], "status": lambda f: f["share"], "added": lambda f: f["added"]}


def sort_args(src, fields=None):
    """{"sort": field, "dir": "asc"|"desc"} from a request, or {} for no sorting (the
    default order)."""
    fields = fields or SORT_FIELDS
    if src.get("sort") not in fields:
        return {}
    return {"sort": src["sort"], "dir": "desc" if src.get("dir") == "desc" else "asc"}


def next_sort(field, sort):
    """Clicking a column header cycles: ascending -> descending -> no sorting."""
    if sort.get("sort") != field:
        return {"sort": field, "dir": "asc"}
    return {"sort": field, "dir": "desc"} if sort["dir"] == "asc" else {"sort": "none"}


def sort_arrow(field, sort):
    return ("▲" if sort["dir"] == "asc" else "▼") if sort.get("sort") == field else ""


def sort_rows(rows, keyfunc, descending, tiebreak=None):
    """Sort with rows lacking a value last in both directions; equal values keep the
    order of tiebreak (e.g. by name), since Python's sort is stable."""
    if tiebreak:
        rows = sorted(rows, key=tiebreak)
    have = [r for r in rows if keyfunc(r) is not None]
    missing = [r for r in rows if keyfunc(r) is None]
    return sorted(have, key=keyfunc, reverse=descending) + missing


def tree_level(path, filters, queued, offset=0, sort=None):
    """Direct subfolders and files of `path` ("" = top level) that match the filters.
    Each folder counts its files: optimized (status h265: optimized codec or name),
    unknown (not probed) and other. Returns (folders, files of the requested page,
    number of files in total)."""
    clause, params = filter_sql(filters, path)
    plen = len(path) + 1 if path else 0
    sort = sort or {"sort": "name", "dir": "asc"}          # no sorting = by name
    where = clause + (" AND " if clause else " WHERE ")
    start = plen + 1                            # first character after "path/"
    folders = {}
    with closing(db()) as con:
        # Subfolders: SQLite groups by the next path part and counts (fast for 70,000+ files)
        q_sum = "0"
        if queued:
            con.execute("CREATE TEMP TABLE q (key TEXT PRIMARY KEY)")
            con.executemany("INSERT OR IGNORE INTO q VALUES (?)", [(k,) for k in queued])
            q_sum = "SUM(key IN (SELECT key FROM q))"
        for head, count, size, optimized, unknown, in_queue, added in con.execute(
                f"SELECT substr(key, ?, instr(substr(key, ?), '/') - 1), COUNT(*), COALESCE(SUM(size), 0), "
                f"SUM(status = 'h265'), SUM(status != 'h265' AND video_codec IS NULL), {q_sum}, MAX(added) "
                f"FROM files{where}instr(substr(key, ?), '/') > 0 GROUP BY 1", [start, start] + params + [start]):
            folders[head] = {"name": head, "path": f"{path}/{head}" if path else head, "count": count,
                             "size": size, "optimized": optimized, "unknown": unknown, "selectable": count - in_queue,
                             "added": added}
        # Files directly in this folder, sorted (only their rows are loaded)
        cols = ("key", "size", "status", "video_codec", "bitrate", "duration", "width", "height", "added")
        here = [dict(zip(cols, r)) for r in con.execute(
            f"SELECT {', '.join(cols)} FROM files{where}instr(substr(key, ?), '/') = 0", params + [start])]
        keys = [r["key"] for r in sort_rows(here, SORT_FIELDS[sort["sort"]][1], sort["dir"] == "desc",
                                             tiebreak=SORT_FIELDS["name"][1])]
        page = keys[offset:offset + PAGE_SIZE]
        rows = {}
        if page:
            cols = ", ".join(enc.FILE_COLUMNS)
            marks = ", ".join("?" * len(page))
            rows = {r[0]: dict(zip(enc.FILE_COLUMNS, r))
                    for r in con.execute(f"SELECT {cols} FROM files WHERE key IN ({marks})", page)}
    if not path:                                # top level: shares, shown by name
        for prefix, (name, folder) in root_folders().items():
            if prefix in folders:
                folders[prefix]["name"] = name
                if folder != name:
                    folders[prefix]["note"] = folder
    for f in folders.values():
        f["other"] = f["count"] - f["optimized"] - f["unknown"]
        f["share"] = f["optimized"] / f["count"] * 100 if f["count"] else 0
    files = [dict(rows[k], name=k[plen:]) for k in page if k in rows]
    ordered = sorted(folders.values(), key=lambda f: natural(f["name"]))
    if sort["sort"] in FOLDER_SORT:
        ordered = sort_rows(ordered, FOLDER_SORT[sort["sort"]], sort["dir"] == "desc")
    elif sort["sort"] == "name" and sort["dir"] == "desc":
        ordered.reverse()
    return ordered, files, len(keys)


def activity_version():
    """Changes when the file being encoded or the results change (folder cache version)."""
    cur = enc.current_progress()
    try:
        size = enc.RESULTS_FILE.stat().st_size
    except OSError:
        size = 0
    return (cur["key"] if cur else None, size)


def level_context(path, filters, queued, offset=0, sort=None, dir_offset=0):
    """One folder level: a page of subfolders (dir_offset) and a page of files (offset).
    Large levels are paged so the browser never builds thousands of rows at once."""
    sort = sort or {}
    folders, files, total = tree_level(path, filters, queued, offset, sort)
    nxt = offset + PAGE_SIZE
    more = url_for("files_folder", path=path, offset=nxt, rows=1, **filters, **sort) if nxt < total else None
    dir_nxt = dir_offset + FOLDER_PAGE
    more_dirs = (url_for("files_folder", path=path, dirs=dir_nxt, **filters, **sort)
                 if path and dir_nxt < len(folders) else None)
    total_dirs = len(folders)
    if path:                                    # the top level (shares) is never paged
        folders = folders[dir_offset:dir_nxt]
    # Finished encodes, for the Replace button. A file whose encode is in the output folder is
    # awaiting review even if the database still says "queued": only the next scan updates
    # that status (the worker doesn't write to the database).
    if any(f["status"] in ("waiting", "queued") for f in files):
        encoded = enc.encoded_files(enc.first_existing(enc.ENCODED_CANDIDATES))
        pending = enc.pending_replaces()
        for f in files:
            f["encoded"] = encoded.get(Path(f["name"]).stem.lower()) if f["status"] in ("waiting", "queued") else None
            if f["encoded"]:
                f["status"] = "waiting"
            f["replacing"] = f["key"] in pending
    # The file being encoded is shown as such, and a file whose last encode failed is marked
    # (a later success or a rescan to "waiting" clears it: only the latest result counts).
    cur = enc.current_progress()
    results = enc.load_results() if files else {}
    for f in files:
        f["encoding"] = bool(cur) and cur["key"] == f["key"]
        r = results.get(f["key"])
        f["failed"] = bool(r and r[0] == "FAIL" and f["status"] == "offer")
    return {"folders": folders, "files": files, "total_files": total, "shown_files": min(nxt, total),
            "more_url": more, "more_dirs_url": more_dirs, "shown_dirs": min(dir_nxt, total_dirs),
            "total_dirs": total_dirs, "filters": filters, "queued": queued,
            "status_labels": enc.STATUS_LABELS, "sort": sort, "sort_links": sort_links(filters, sort),
            "link_args": dict(filters, **sort),      # query of folder URLs: filters + sort
            "folder_query": urllib.parse.urlencode(dict(filters, **sort), doseq=True)}


def sort_links(filters, sort):
    """Column header links: field -> (URL of the Files page with the next sort state, arrow or "")."""
    return {field: (url_for("files", **filters, **next_sort(field, sort)), sort_arrow(field, sort))
            for field in SORT_FIELDS}


def library_summary():
    """Totals, codec and status counts and unscanned shares; cached until the database changes."""
    def compute():
        with closing(db()) as con:
            total, total_size, opt_size = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(CASE WHEN status = 'h265' THEN size END), 0) "
                "FROM files").fetchone()
            codecs = con.execute(f"SELECT COALESCE(video_codec, '{UNKNOWN_CODEC}'), COUNT(*) FROM files "
                                 "GROUP BY 1 ORDER BY 2 DESC").fetchall()
            statuses = dict(con.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        unscanned = [enc.SHARE_NAMES[p] for p, i in share_info().items() if not i["files"]]
        return total, total_size, opt_size, codecs, statuses, unscanned
    return cached(("summary", tuple(enc.ROOT_CANDIDATES)), enc.db_version(), compute)


def sort_state(src, name, fields=None):
    """The list's sort: from the request if it sets one (sort=none = off), otherwise the
    one remembered in the session, so it survives switching pages and reloading."""
    if "sort" in src:
        sort = sort_args(src, fields)
        session[f"sort:{name}"] = sort
        return sort
    return sort_args(session.get(f"sort:{name}", {}), fields)


@app.route("/files")
def files():
    filters = file_filters(request.args)
    sort = sort_state(request.args, "files")
    empty = render_template("files.html", total=0, filters=filters, sort=sort)
    if g.config_error:
        return empty
    total, total_size, opt_size, codecs, statuses, unscanned = library_summary()
    if not total:
        return empty
    if filters:
        with closing(db()) as con:
            clause, params = filter_sql(filters)
            shown = con.execute(f"SELECT COUNT(*) FROM files{clause}", params).fetchone()[0]
    else:
        shown = total
    queued = enc.pending_keys()
    # Folder contents only change with the database or the queue: the browser keeps loaded
    # folders for this version (files.js), so reopening them after a reload needs no request
    version = hashlib.sha1(repr((STARTED, enc.db_version(), sorted(queued), activity_version())).encode()).hexdigest()[:12]
    if filters:
        level = level_context("", filters, queued, sort=sort)
    else:                                       # the unfiltered top level is the same until data changes
        level = cached(("top", tuple(sorted(sort.items())), hash(frozenset(queued))), enc.db_version(),
                       lambda: level_context("", filters, queued, sort=sort))
    return render_template(
        "files.html", total=total, total_size=total_size, shown=shown,
        optimized_share=opt_size / total_size * 100 if total_size else 0, codecs=codecs,
        statuses=statuses, q=filters.get("q", ""), codec=filters.get("codec", []),
        status=filters.get("status", ""), filtered=bool(filters),
        added=filters.get("added", request.args.get("added", "") if request.args.get("added") in ADDED_RANGES else ""),
        date_from=filters.get("from", ""), date_to=filters.get("to", ""),
        added_ranges=ADDED_RANGES, added_error=added_problem(request.args),
        codec_groups=[(k, label, sum(n for c, n in codecs if codec_class(c) == k.split(":")[1]))
                      for k, label in CODEC_GROUPS.items()],
        codec_summary=codec_summary(filters.get("codec", [])),
        last_scan=enc.last_scan(), ffprobe=bool(enc.FFPROBE), preset_list=preset_choices(),
        unscanned=unscanned, folder_version=version, **level)


@app.route("/files/folder")
def files_folder():
    """Content of one folder for the tree: subfolders and the first page of files,
    or with rows=1 only the next page of file rows ("Show more")."""
    if g.config_error:
        abort(409, g.config_error)
    path = request.args.get("path", "").strip("/")
    offset = max(int(request.args.get("offset", 0) or 0), 0)
    dir_offset = max(int(request.args.get("dirs", 0) or 0), 0)
    ctx = level_context(path, file_filters(request.args), enc.pending_keys(), offset, sort_args(request.args),
                        dir_offset)
    if request.args.get("rows"):
        resp = app.make_response(squeeze(render_template("_file_rows.html", **ctx)))
        resp.headers["X-Next"] = ctx["more_url"] or ""
        return resp
    if dir_offset:                              # "Show more folders": only the next folders
        ctx["files"] = []
    return squeeze(render_template("_level.html", **ctx))


def squeeze(html):
    """Collapse the template's indentation between tags (renders the same, parses faster)."""
    return re.sub(r">\s+<", "> <", html)


def safe_next(default):
    nxt = request.form.get("next", "")             # only paths on this site, no external addresses
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default)


@app.route("/queue", methods=["POST"])
def queue():
    """Queue the selected files, plus every file (matching the active filters) in the
    selected folders."""
    keys = request.form.getlist("key")
    folders = [f.strip("/") for f in request.form.getlist("folder")]
    filters = file_filters(request.form)
    with closing(db()) as con:
        if keys:
            marks = ", ".join("?" * len(keys))
            known = {k for (k,) in con.execute(f"SELECT key FROM files WHERE key IN ({marks})", keys)}
            if set(keys) - known:                   # only files from the last scan can be queued
                abort(400, "Unknown file selected. Scan again and retry.")
        for folder in folders:
            clause, params = filter_sql(filters, folder)
            found = [k for (k,) in con.execute(f"SELECT key FROM files{clause}", params)]
            if not folder or not found:
                abort(400, "Unknown folder selected. Scan again and retry.")
            keys += sorted(found, key=natural)
    preset = request.form.get("preset", "").strip() or None
    if preset and not presets.exists(preset):
        abort(400, "Unknown preset.")
    if not keys:
        flash("No files selected.", "error")
    else:
        chosen = enc.write_batch(keys, preset)
        skipped = len(set(keys)) - len(chosen)
        msg = f"{len(chosen):,} file(s) queued" + (f" with preset \"{presets.name_of(preset)}\"." if preset else ".")
        if skipped:
            msg += f" {skipped:,} already in the queue."
        flash(msg, "ok" if chosen else "error")
    return safe_next(url_for("files"))


QUEUE_PAGE = 100                                # queue entries per page on the Status page


def replace_info(rows):
    """Add "encoded" (finished encode still in the output folder: Replace… possible) and
    "replaced" (already replaced by a replace job) to rows of successfully encoded files."""
    encoded = enc.encoded_files(enc.first_existing(enc.ENCODED_CANDIDATES))
    replaced = enc.replaced_keys()
    pending = enc.pending_replaces()
    for r in rows:
        r["replacing"] = r["key"] in pending
        if r.get("state", r.get("status")) == "OK":
            r["encoded"] = encoded.get(Path(r["key"]).stem.lower())
            r["replaced"] = not r["encoded"] and r["key"] in replaced
            r["original"] = r["key"].rsplit("/", 1)[-1]
    return rows


TAB_ICONS = {"Summary": "summary", "Dimensions": "crop", "Filters": "sliders", "Video": "film",
             "Audio": "speaker", "Subtitles": "captions", "More": "dots"}      # preset editor tabs (_icons.html)
VIEWS = {"queue": "Queue", "review": "Awaiting review", "history": "History", "originals": "Kept originals"}   # tabs of the Status page

# Sortable columns per tab: field -> (label, sort key of a row). No sorting = the tab's own order:
# processing order (queue), by file (awaiting review), newest first (history).
VIEW_SORT = {
    "queue": {
        "file":   ("File", lambda i: natural(enc.short(i["key"]))),
        "preset": ("Preset", lambda i: (presets.name_of(i["preset"]) if i["preset"] else "").lower()),
        "state":  ("State", lambda i: list(enc.QUEUE_STATES).index(i["state"]) if i["state"] in enc.QUEUE_STATES else 99),
    },
    "review": {
        "file": ("File", lambda i: natural(enc.short(i["key"]))),
        "size": ("Size", lambda i: i["size"]),
        "at":   ("Encoded", lambda i: i["at"]),
    },
    "originals": {
        "file": ("File", lambda i: natural(i["key"])),
        "size": ("Size", lambda i: i["size"]),
        "kept": ("Kept since", lambda i: i["kept"]),
    },
    "history": {
        "at":     ("Time", lambda i: i["at"]),
        "status": ("Result", lambda i: i["status"]),
        "file":   ("File", lambda i: natural(enc.short(i["key"]))),
    },
}
HISTORY_RESULTS = ("OK", "FAIL", "STOPPED", "MISSING", "EXISTS", "REPLACED")


def status_view(src):
    view = src.get("view", "queue")
    return view if view in VIEWS else "queue"


def awaiting_review():
    """Files whose finished encode waits in the output folder, also from runs that are over,
    as [{"key", "at" (when the encode was finished), "size" (of the encode), "original_size"
    (or None), "state": "OK"}], not dismissed. The scan is cached until the
    database or the output folder changes."""
    enc_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    try:
        folder_version = enc_dir.stat().st_mtime_ns if enc_dir else 0
    except OSError:
        folder_version = 0

    def compute():
        encoded = enc.encoded_files(enc_dir)
        if not encoded:
            return []
        with closing(db()) as con:
            originals = {k: size for k, size in con.execute("SELECT key, size FROM files WHERE status != 'h265'")
                         if Path(k).stem.lower() in encoded}
        out = []
        for k in sorted(originals, key=natural):
            try:
                st = (enc_dir / encoded[Path(k).stem.lower()]).stat()
            except OSError:
                continue
            out.append({"key": k, "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "size": st.st_size, "original_size": originals[k], "state": "OK"})
        return out
    found = cached(("awaiting", str(enc_dir)), (enc.db_version(), folder_version), compute)
    dismissed = enc.dismissed_map()
    return [dict(f, preset="") for f in found if dismissed.get(f["key"], "") < f["at"]]


_KEPT_GEN = [0]


def kept_list():
    """enc.kept_originals(), kept for 30 seconds (walking the folder isn't free) and until
    something is deleted."""
    return cached("kept", (int(time.time() // 30), _KEPT_GEN[0]), enc.kept_originals)


def entry_parts(key, kept=False):
    """(where, file name) of a Status entry, shown on two lines: share and top folder (like
    enc.short()), or for a kept original (a path inside .replaced/) its folder."""
    parts = key.split("/")
    if kept:
        return (parts[-2] if len(parts) > 1 else ""), parts[-1]
    share = enc.SHARE_NAMES.get(parts[0], parts[0])
    return (f"{share} › {parts[1]}" if len(parts) > 2 else share), parts[-1]


def kept_short(rel):
    parts = rel.split("/")
    return f"{parts[-2]} › {parts[-1]}" if len(parts) > 1 else rel


def kept_info(rows):
    """For the rows of one page: how big the replacing file is now, and whether it is still in
    the library (a warning, not a block: it may just have been renamed)."""
    for r in rows:
        r["new_size"], r["warn"] = None, ""
        if not r["new_key"]:
            r["warn"] = "unknown"               # no replace job on record (older install, or the log was cleared)
            continue
        prefix, _, rel = r["new_key"].partition("/")
        root = enc.first_existing(enc.ROOT_CANDIDATES.get(prefix, []))
        if root is None:
            r["warn"] = "offline"
            continue
        try:
            r["new_size"] = (root / rel).stat().st_size
        except OSError:
            r["warn"] = "missing"


def queue_rows(view, state):
    """All rows of a tab (before sorting and paging) and the counts shown in the tabs."""
    items = enc.queue_items()
    has_run = any(i["run"] for i in items)
    n = 0
    for item in items:                          # processing order of the run and queue
        n += 1
        item["n"] = n
        item["next_run"] = has_run and not item["run"]      # waits for a new worker run
    active = [i for i in items if i["state"] in ("encoding", "waiting", "removed")]
    review = awaiting_review()
    history = enc.history_entries(state if view == "history" else "")
    kept = kept_list()
    counts = Counter(i["state"] for i in items)
    counts.update({"queue": counts["encoding"] + counts["waiting"], "review": len(review), "originals": len(kept),
                   "history": len(history) if view == "history" and not state else len(enc.history_entries())})
    if view == "review":
        return review, counts
    if view == "originals":
        return kept, counts
    if view == "history":
        return history, counts
    return [i for i in active if not state or i["state"] == state], counts


def queue_view(view="queue", state="", offset=0, sort=None):
    """One tab of the Status page: counts for the tab labels and one page of rows
    (optionally only one state or result, optionally sorted)."""
    sort = sort or {}
    fields = VIEW_SORT[view]
    shown, counts = queue_rows(view, state)
    if sort:
        shown = sorted(shown, key=fields[sort["sort"]][1], reverse=sort["dir"] == "desc")
    # Waiting entries can be reordered in the queue's own order; a divider shows where the next run starts
    can_move = view == "queue" and not sort and state in ("", "waiting")
    if can_move and any(i["run"] for i in shown):
        first = next((i for i in shown if i["next_run"] and i["state"] == "waiting"), None)
        if first:
            first["first_next"] = True
    page = shown[offset:offset + QUEUE_PAGE]
    names = {}
    rows = []
    for item in page:
        pid = item.get("preset")
        if pid and pid not in names:
            names[pid] = presets.name_of(pid)
        where, file = entry_parts(item["key"], kept=view == "originals")
        rows.append(dict(item, short=kept_short(item["key"]) if view == "originals" else enc.short(item["key"]),
                         where=where, file=file, preset_name=names.get(pid, "")))
    if view == "review":
        replace_info(rows)
    elif view == "originals":
        kept_info(rows)
    nxt = offset + QUEUE_PAGE
    return {"rows": rows, "counts": counts, "shown_total": len(shown), "view": view,
            "more_url": url_for("status_queue", view=view, state=state, offset=nxt, **sort) if nxt < len(shown) else None,
            "states": enc.QUEUE_STATES, "state": state, "sort": sort, "can_move": can_move,
            "sort_links": {f: (url_for("status", view=view, state=state, **next_sort(f, sort)), sort_arrow(f, sort))
                           for f in fields},
            "fields": {f: v[0] for f, v in fields.items()},
            "keep_days": enc.history_keep_days(), "history_results": HISTORY_RESULTS,
            "kept_total": sum(i["size"] for i in shown) if view == "originals" else 0,
            "kept_auto_days": enc.keep_originals_days()}


def live_status():
    """Small snapshot for the Status page's live updates."""
    cur = enc.current_progress()
    states = Counter(i["state"] for i in enc.queue_items())
    stop = enc.stop_requested()
    return {
        "current": dict(cur, short=enc.short(cur["key"]), where=entry_parts(cur["key"])[0],
                        file=entry_parts(cur["key"])[1]) if cur else None,
        "stopping": stop[0] if stop and cur and stop[1] == cur["key"] else None,
        "paused": enc.is_paused(),
        "can_stop": enc.worker_can_stop(),
        "counts": {"encoding": states["encoding"], "waiting": states["waiting"],
                   "queue": states["encoding"] + states["waiting"],
                   "review": len(awaiting_review()), "history": len(enc.history_entries()),
                   "originals": len(kept_list())},
        "scan": dict({k: SCAN[k] for k in ("running", "label", "summary", "error")},
                     started=(to_datetime(SCAN["started"]) or datetime.now()).isoformat() if SCAN["started"] else None),
    }


@app.route("/replace/suggest")
def replace_suggest():
    """Suggested file name for replacing key with its finished encode (codec read with ffprobe)."""
    key = request.args.get("key", "")
    enc_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    encoded = enc.encoded_files(enc_dir).get(Path(key).stem.lower())
    if not encoded:
        return {"error": "No finished encode of this file in the output folder."}, 404
    codec = enc.probe(enc_dir / encoded)["video_codec"] if enc.FFPROBE else None
    quality = enc.encode_quality(key)
    return {"name": enc.suggest_name(key.rsplit("/", 1)[-1], encoded, codec, quality), "codec": codec, "quality": quality}


BULK_REPLACE_MAX = 500                          # files per bulk replace


@app.route("/replace/preview", methods=["POST"])
def replace_preview():
    """Files for a bulk replace (JSON): the ticked files and folders of the Files page
    (folders expanded with the submitted filters) or, with all=1, every file awaiting
    review; each with its finished encode and suggested name. Files without a finished
    encode, or with a replace already pending, are left out."""
    with closing(db()) as con:
        if request.form.get("all"):             # every file with an encode, whatever the status says
            keys = [k for (k,) in con.execute("SELECT key FROM files WHERE status != 'h265'")]
        else:
            keys = request.form.getlist("key")
            filters = file_filters(request.form)
            for folder in (f.strip("/") for f in request.form.getlist("folder")):
                clause, params = filter_sql(filters, folder)
                keys += [k for (k,) in con.execute(f"SELECT key FROM files{clause}", params)]
    keys = sorted(set(keys), key=natural)
    enc_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    encoded = enc.encoded_files(enc_dir)
    pending = enc.pending_replaces()
    rows = [(k, encoded[Path(k).stem.lower()]) for k in keys
            if Path(k).stem.lower() in encoded and k not in pending][:BULK_REPLACE_MAX]
    with ThreadPoolExecutor(enc.PROBE_WORKERS) as pool:   # the codec decides the suggested tag
        codecs = list(pool.map(lambda e: enc.probe(enc_dir / e)["video_codec"] if enc.FFPROBE else None,
                               [e for _, e in rows]))
    items = [{"key": k, "short": enc.short(k), "original": k.rsplit("/", 1)[-1], "encoded": e,
              "suggested": enc.suggest_name(k.rsplit("/", 1)[-1], e, c, enc.encode_quality(k))}
             for (k, e), c in zip(rows, codecs)]
    return {"items": items, "left_out": len(keys) - len(items), "limit": BULK_REPLACE_MAX}


@app.route("/replace/bulk", methods=["POST"])
def replace_bulk():
    """Replace several originals at once (key and new_name lists in the same order). All
    rows are checked first; with any problem nothing is started (JSON: errors per key)."""
    keys, names = request.form.getlist("key"), [n.strip() for n in request.form.getlist("new_name")]
    if not keys or len(keys) != len(names) or len(keys) > BULK_REPLACE_MAX:
        return {"error": "Nothing to replace."}, 400
    with closing(db()) as con:
        marks = ", ".join("?" * len(keys))
        known = {k for (k,) in con.execute(f"SELECT key FROM files WHERE key IN ({marks})", keys)}
    encoded = enc.encoded_files(enc.first_existing(enc.ENCODED_CANDIDATES))
    pending = enc.pending_replaces()
    errors, targets, jobs = {}, {}, []
    for key, name in zip(keys, names):
        enc_name = encoded.get(Path(key).stem.lower())
        target = (key.rsplit("/", 1)[0], name.lower())
        problem = ("Unknown file. Scan again and retry." if key not in known else
                   "Already being replaced." if key in pending else
                   "No finished encode of this file in the output folder." if not enc_name else
                   enc.check_new_name(name, enc_name) or
                   ("Same name as another file in this list." if target in targets else None))
        if problem:
            errors[key] = problem
        targets[target] = key
        jobs.append((key, enc_name, name))
    if errors:
        return {"errors": errors}, 400
    keep = bool(request.form.get("keep_original"))
    return {"jobs": [{"key": k, "status_url": url_for("replace_state", job_id=enc.request_replace(k, e, n, keep))}
                     for k, e, n in jobs]}


@app.route("/replace", methods=["POST"])
def replace():
    """Ask the worker to replace an original with its finished encode (JSON)."""
    key = request.form.get("key", "")
    new_name = request.form.get("new_name", "").strip()
    with closing(db()) as con:
        row = con.execute("SELECT status FROM files WHERE key = ?", (key,)).fetchone()
    if not row:
        return {"error": "Unknown file. Scan again and retry."}, 400
    encoded = enc.encoded_files(enc.first_existing(enc.ENCODED_CANDIDATES)).get(Path(key).stem.lower())
    if not encoded:
        return {"error": "No finished encode of this file in the output folder."}, 400
    problem = enc.check_new_name(new_name, encoded)
    if problem:
        return {"error": problem}, 400
    job_id = enc.request_replace(key, encoded, new_name, keep_original=bool(request.form.get("keep_original")))
    return {"id": job_id, "status_url": url_for("replace_state", job_id=job_id)}


@app.route("/replace/<job_id>")
def replace_state(job_id):
    st = enc.replace_status(job_id)
    if st.get("state") == "done" and not st.get("synced"):
        if sync_replaces():
            st["synced"] = True
        else:
            st["message"] += " The list updates once the running scan is finished."
    return st


@app.route("/status")
def status():
    cfg = enc.parse_config(enc.CONFIG_FILE) if enc.CONFIG_FILE.exists() else {}
    view = status_view(request.args)
    state = request.args.get("state", "")
    cur = enc.current_progress()
    stop = enc.stop_requested()
    return render_template("status.html", cur=cur, paused=enc.is_paused(), can_stop=enc.worker_can_stop(),
                           stopping=stop[0] if stop and cur and stop[1] == cur["key"] else None,
                           q=queue_view(view, state, sort=sort_state(request.args, f"queue:{view}", VIEW_SORT[view])),
                           views=VIEWS, auto_interval=scan_interval(cfg),
                           auto_next=next_auto_scan(cfg), short=enc.short, entry_parts=entry_parts,
                           last_scan=enc.last_scan())


@app.route("/status/live")
def status_live():
    return live_status()


@app.route("/status/queue")
def status_queue():
    """One page of rows of a tab (for "Show more", the filter and refreshing)."""
    view = status_view(request.args)
    state = request.args.get("state", "")
    offset = max(int(request.args.get("offset", 0) or 0), 0)
    data = queue_view(view, state, offset, sort_args(request.args, VIEW_SORT[view]))
    resp = app.make_response(render_template("_queue_rows.html", **data))
    resp.headers["X-Next"] = data["more_url"] or ""
    resp.headers["X-Shown-Total"] = str(data["shown_total"])
    return resp


@app.route("/queue/remove", methods=["POST"])
def queue_remove():
    """Remove waiting entries from the queue: the selected ones, or with all=1 every one."""
    keys = None if request.form.get("all") else request.form.getlist("key")
    if keys == []:
        flash("No entries selected.", "error")
    else:
        removed = enc.remove_queued(keys)
        flash(f"{len(removed):,} entr{'y' if len(removed) == 1 else 'ies'} removed from the queue."
              if removed else "Nothing removed: the entries aren't waiting any more.", "ok" if removed else "error")
    return safe_next(url_for("status"))


@app.route("/queue/move", methods=["POST"])
def queue_move():
    """Change the order of waiting entries: op = top, bottom, up, down (for the given keys)
    or before (in front of `target`, "" = at the end). With json=1 the answer is JSON
    for the page's drag and arrow buttons, otherwise the page is shown again."""
    op = request.form.get("op", "")
    if op not in enc.MOVES:
        abort(400, "Unknown move.")
    keys = request.form.getlist("key")
    waiting = [i["key"] for i in enc.queue_items() if i["state"] == "waiting"]
    moved = [k for k in keys if k in waiting]
    if not moved:
        ok, msg = False, "Nothing moved: the entries aren't waiting any more." if keys else "No entries selected."
    else:
        n = enc.reorder_queued(enc.moved_order(waiting, op, moved, request.form.get("target", "")))
        ok, msg = True, (f"{len(set(moved)):,} entr{'y' if len(set(moved)) == 1 else 'ies'} moved." if n
                         else "Already in that place.")
    if request.form.get("json"):
        return {"ok": ok, "message": msg}, 200 if ok else 409
    flash(msg, "ok" if ok else "error")
    return safe_next(url_for("status"))


@app.route("/status/dismiss", methods=["POST"])
def status_dismiss():
    """Hide entries of the Status page (they stay on disk): the selected keys, all=1 for
    everything the tab shows (with its result filter), or older_than=N days (History)."""
    view, state = status_view(request.form), request.form.get("state", "")
    older = request.form.get("older_than", "")
    if older:
        if not older.isdigit():
            abort(400, "Invalid number of days.")
        rows = enc.history_entries(state, older_than=int(older))
        upto = {}
        for r in rows:                              # hide each file up to its newest old entry only
            upto[r["key"]] = max(upto.get(r["key"], ""), r["at"])
        enc.dismiss(upto, upto)
        n = len(upto)
    else:
        if request.form.get("all"):
            keys = [r["key"] for r in (awaiting_review() if view == "review" else enc.history_entries(state))]
        else:
            keys = request.form.getlist("key")
        if not keys:
            flash("No entries selected.", "error")
            return safe_next(url_for("status", view=view))
        enc.dismiss(keys)
        n = len(set(keys))
    flash(f"{n:,} entr{'y' if n == 1 else 'ies'} hidden." if n else "Nothing to hide.", "ok" if n else "error")
    return safe_next(url_for("status", view=view))


@app.route("/originals/delete", methods=["POST"])
def originals_delete():
    """Permanently delete kept originals: the selected ones (paths inside .replaced/), or
    with older_than=N all that were kept for more than N days."""
    older = request.form.get("older_than", "")
    if older:
        if not older.isdigit() or int(older) < 1:
            abort(400, "Invalid number of days.")
        count, freed = enc.purge_kept(int(older))
    else:
        keys = request.form.getlist("key")
        if not keys:
            flash("No entries selected.", "error")
            return safe_next(url_for("status", view="originals"))
        count, freed = enc.delete_kept(keys)
    _KEPT_GEN[0] += 1
    flash(f"{count:,} original{'' if count == 1 else 's'} deleted, {fmt_size(freed)} freed." if count
          else "Nothing deleted: no matching originals found.", "ok" if count else "error")
    return safe_next(url_for("status", view="originals"))


@app.route("/queue/again", methods=["POST"])
def queue_again():
    """Queue files whose last encode failed or was stopped again, with the preset they had."""
    keys = request.form.getlist("key")
    if not keys:
        flash("No entries selected.", "error")
    else:
        queued, left = enc.requeue(keys)
        msg = f"{len(queued):,} file(s) queued again." if queued else "Nothing queued."
        if left:
            msg += f" {left:,} not queued (only failed or stopped files that are still in the library and not queued yet)."
        flash(msg, "ok" if queued else "error")
    return safe_next(url_for("status", view="history"))


@app.route("/encode/stop", methods=["POST"])
def encode_stop():
    """Stop the running encode: mode=skip goes on with the next file, mode=all pauses the queue."""
    mode, key = request.form.get("mode", ""), request.form.get("key", "")
    problem = enc.request_stop(mode, key)
    if problem:
        flash(f"Nothing stopped: {problem}", "error")
    else:
        flash(f"Stopping {enc.short(key)} …" + (" The rest of the queue will be paused." if mode == "all" else
              " The worker goes on with the next file.") + " This takes a few seconds.", "ok")
    return redirect(url_for("status"))


@app.route("/encode/resume", methods=["POST"])
def encode_resume():
    enc.resume_queue()
    flash("Queue resumed. The worker starts within a minute.", "ok")
    return redirect(url_for("status"))


@app.route("/results/clear", methods=["POST"])
def results_clear():
    n = enc.clear_results()
    flash(f"History data deleted ({n:,} result(s)).", "ok")
    return safe_next(url_for("status", view="history"))


@app.route("/scan", methods=["POST"])
def start_scan():
    """Scan all shares (new or changed files only), or with share=shareN one share:
    an initial scan of a new share, or with share=shareN:force a full rescan that
    re-reads every file of the share with ffprobe."""
    if g.config_error:
        flash(g.config_error, "error")
        return redirect(url_for("settings", section="library"))
    path = request.form.get("path", "").strip().strip("/")
    if path:                                    # one file or folder: re-read it with ffprobe
        if path.split("/", 1)[0] not in enc.ROOT_CANDIDATES or ".." in path.split("/"):
            abort(400, "Unknown file or folder.")
        thread = start_background_scan(f"Check of {enc.short(path) if '/' in path else enc.SHARE_NAMES[path]}",
                                       force=True, path=path)
    else:
        prefix, _, mode = request.form.get("share", "").partition(":")
        if prefix and prefix not in enc.ROOT_CANDIDATES:
            abort(400, "Unknown share.")
        only, force = ([prefix] if prefix else None), mode == "force"
        name = enc.SHARE_NAMES.get(prefix, prefix)
        label = f"{'Full rescan' if force else 'Scan'} of {name}" if prefix else "Scan of all shares"
        thread = start_background_scan(label, only, force)
    wants_json = request.accept_mimetypes.best == "application/json"
    if thread is None:
        if wants_json:
            return {"done": False, "error": "A scan is already running. Try again when it's finished."}, 409
        flash("A scan is already running.", "error")
        return safe_next(url_for("status"))
    if path:
        thread.join(timeout=15)                 # small checks finish right away; show their result
        if wants_json:
            return {"done": not SCAN["running"], "message": SCAN["summary"] if not SCAN["running"] else
                    f"{SCAN['label']} is still running (large folder) – see the Status page.",
                    "error": SCAN["error"]}
        if SCAN["running"]:
            flash(f"{SCAN['label']} is still running – see the Status page.", "ok")
        elif SCAN["error"]:
            flash(SCAN["error"], "error")
        else:
            flash(SCAN["summary"], "ok")
    else:
        flash(f"{SCAN['label']} started. Files are read with ffprobe, which can take a while"
              + (" (about 0.1 s per file)." if force or prefix else "."), "ok")
    return safe_next(url_for("status"))


# ─── Stats ────────────────────────────────────────────────────────────────────

CLASS_SERIES = [("good", "Optimized (H.265, AV1)"), ("other", "Other codecs"), ("unknown", "Not probed")]
GB = 1024 ** 3
BITRATE_BINS = [(0, 2, "< 2"), (2, 4, "2–4"), (4, 8, "4–8"), (8, 15, "8–15"),
                (15, 25, "15–25"), (25, 40, "25–40"), (40, math.inf, "≥ 40")]       # Mbit/s
SIZE_BINS = [(0, 1, "< 1"), (1, 2, "1–2"), (2, 4, "2–4"), (4, 8, "4–8"),
             (8, 15, "8–15"), (15, 30, "15–30"), (30, math.inf, "≥ 30")]           # GB


def nice_ceil(v):
    """Round up to 1, 2, 2.5 or 5 × 10^n for a clean axis maximum."""
    if v <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(v))
    return next(m * exp for m in (1, 2, 2.5, 5, 10) if m * exp >= v)


def chart(title, subtitle, kind, unit, series, rows):
    """rows: [(label, {series_key: value})]. Adds widths and display values for the template.
    kind: bars (horizontal) or columns (vertical); unit: count or size."""
    fmt = fmt_count if unit == "count" else fmt_size
    used = [(k, name) for k, name in series if any(v.get(k) for _, v in rows)]
    totals = [sum(v.values()) for _, v in rows]
    top = max(totals, default=0)
    axis_max = nice_ceil(top) if kind == "columns" and unit == "count" else top or 1
    out = []
    for (label, values), total in zip(rows, totals):
        segs = [{"cls": k, "series": name, "value": values.get(k, 0), "display": fmt(values.get(k, 0)),
                 "pct": values.get(k, 0) / axis_max * 100,
                 "share": values.get(k, 0) / total * 100 if total else 0}
                for k, name in used if values.get(k)]
        out.append({"label": label, "segments": segs, "total": total, "display": fmt(total),
                    "pct": total / axis_max * 100})
    ticks = [(fmt(round(axis_max * f)), f * 100) for f in (0, 0.5, 1)] if kind == "columns" else []
    return {"title": title, "subtitle": subtitle, "kind": kind, "series": used, "rows": out,
            "ticks": ticks, "id": re.sub(r"\W+", "-", title.lower())}


def bin_case(column, bins, scale):
    """SQL CASE that returns the index of the bin (lo <= value/scale < hi), -1 for no value."""
    parts = [f"WHEN {column} < {hi * scale} THEN {i}" for i, (_, hi, _) in enumerate(bins) if hi != math.inf]
    return f"CASE WHEN {column} IS NULL OR {column} = 0 THEN -1 {' '.join(parts)} ELSE {len(bins) - 1} END"


RES_CASE = ("CASE WHEN width IS NULL OR height IS NULL THEN 'Unknown' "
            "WHEN width >= 3200 OR height >= 1800 THEN '2160p (4K)' "
            "WHEN width >= 1800 OR height >= 1000 THEN '1080p' "
            "WHEN width >= 1200 OR height >= 700 THEN '720p' ELSE 'SD' END")


@app.route("/stats")
def stats():
    """All numbers are counted by SQLite in one grouped query (a few thousand groups instead
    of every file), so the page stays fast with large libraries."""
    if g.config_error:
        return render_template("stats.html", charts=[], tiles=None)
    with closing(db()) as con:
        groups = con.execute(f"""
            SELECT video_codec, {bin_case('bitrate', BITRATE_BINS, 1e6)}, {bin_case('size', SIZE_BINS, GB)},
                   {RES_CASE}, substr(key, 1, instr(key, '/') - 1), status, COUNT(*), COALESCE(SUM(size), 0)
            FROM files GROUP BY 1, 2, 3, 4, 5, 6""").fetchall()
        if not groups:
            return render_template("stats.html", charts=[], tiles=None)
        good = sorted(enc.OPTIMIZED_CODECS)
        marks = ", ".join("?" * len(good))

        def median_bitrate(where):
            n = con.execute(f"SELECT COUNT(*) FROM files WHERE bitrate > 0 AND {where}", good).fetchone()[0]
            row = con.execute(f"SELECT bitrate FROM files WHERE bitrate > 0 AND {where} ORDER BY bitrate "
                              f"LIMIT 1 OFFSET ?", good + [n // 2]).fetchone() if n else None
            return row[0] if row else None
        median_good = median_bitrate(f"video_codec IN ({marks})")
        median_other = median_bitrate(f"video_codec IS NOT NULL AND video_codec NOT IN ({marks})")

    by_codec_n, by_codec_size = Counter(), Counter()
    bitrate, size, res = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    sections, status = defaultdict(Counter), Counter()
    no_bitrate = videos = total_size = good_count = other_count = good_size = other_size = 0
    for codec, bb, sb, rc, prefix, st, n, total in groups:
        cls = codec_class(codec)
        codec = codec or UNKNOWN_CODEC
        videos += n
        total_size += total
        by_codec_n[codec] += n
        by_codec_size[codec] += total
        if bb < 0:
            no_bitrate += n
        else:
            bitrate[BITRATE_BINS[bb][2]][cls] += n
        size[SIZE_BINS[max(sb, 0)][2]][cls] += n
        res[rc][cls] += n
        sections[enc.SHARE_NAMES.get(prefix, prefix)][cls] += total
        status[st] += n
        if cls == "good":
            good_count, good_size = good_count + n, good_size + total
        elif cls == "other":
            other_count, other_size = other_count + n, other_size + total

    def codec_rows(counter):
        return [(codec_name(c), {codec_class(c): v}) for c, v in counter.most_common()]

    res_order = ["2160p (4K)", "1080p", "720p", "SD", "Unknown"]
    charts = [
        chart("Files by codec", "Number of files per video codec", "bars", "count",
              CLASS_SERIES, codec_rows(by_codec_n)),
        chart("Storage by codec", "Total file size per video codec", "bars", "size",
              CLASS_SERIES, codec_rows(by_codec_size)),
        chart("Bitrate distribution", "Number of files per overall bitrate range, in Mbit/s"
              + (f" ({no_bitrate} file(s) without bitrate not shown)" if no_bitrate else ""),
              "columns", "count", CLASS_SERIES, [(lbl, bitrate.get(lbl, {})) for _, _, lbl in BITRATE_BINS]),
        chart("File size distribution", "Number of files per size range, in GB", "columns", "count",
              CLASS_SERIES, [(lbl, size.get(lbl, {})) for _, _, lbl in SIZE_BINS]),
        chart("Resolution", "Number of files per resolution", "bars", "count",
              CLASS_SERIES, [(r_, res[r_]) for r_ in res_order if r_ in res]),
        chart("Storage by share", "Total file size per share", "bars", "size",
              CLASS_SERIES, sorted(sections.items())),
        chart("Status", "Number of files per status after the last scan", "bars", "count",
              [("single", "Files")],
              [(enc.STATUS_LABELS.get(s_, s_), {"single": n}) for s_, n in status.most_common()]),
    ]

    tiles = {
        "videos": videos, "total_size": total_size,
        "good_share": good_size / total_size * 100 if total_size else 0,
        "good_count": good_count, "other_count": other_count, "other_size": other_size,
        "median_good": median_good, "median_other": median_other,
    }
    return render_template("stats.html", charts=charts, tiles=tiles, last_scan=enc.last_scan())


# ─── Presets ──────────────────────────────────────────────────────────────────

def preset_usage():
    """Preset id -> names of the shares that use it (DEFAULT_PRESET for shares without one)."""
    cfg = enc.parse_config(enc.CONFIG_FILE) if enc.CONFIG_FILE.exists() else {}
    usage = {}
    for sh in enc.shares(cfg):
        usage.setdefault(sh["preset"] or cfg.get("DEFAULT_PRESET", ""), []).append(sh["name"])
    return usage, cfg.get("DEFAULT_PRESET", "")


@app.route("/presets")
def presets_page():
    usage, default = preset_usage()
    rows = [{"id": pid, "name": p.get("PresetName", pid), "description": p.get("PresetDescription", ""),
             "summary": presets.summary(p), "shares": usage.get(pid, []), "default": pid == default}
            for pid, p in presets.all_presets()]
    builtins = {}
    for category, name, description in presets.builtin_presets():
        builtins.setdefault(category, []).append((name, description))
    return render_template("presets.html", rows=rows, builtins=builtins, caps=presets.capabilities())


@app.route("/presets/new", methods=["POST"])
def preset_new():
    source, name = request.form.get("source"), request.form.get("name", "").strip()
    try:
        if source == "builtin":
            builtin = request.form.get("builtin", "")
            preset = presets.from_builtin(builtin, name or builtin)
        elif source == "copy":
            preset = presets.load(request.form.get("copy", ""))
            preset["PresetName"] = name or f"{preset['PresetName']} (copy)"
        elif source == "import":
            upload = request.files.get("file")
            if not upload or not upload.filename:
                raise presets.PresetError("Choose a preset file to import.")
            ids = presets.import_data(upload.read())
            flash(f"Imported {len(ids)} preset(s).", "ok")
            return redirect(url_for("preset_edit", pid=ids[0]) if len(ids) == 1 else url_for("presets_page"))
        else:
            abort(400)
        pid = presets.new_id(preset["PresetName"])
        presets.save(pid, dict(preset, Default=False))
    except presets.PresetError as e:
        flash(str(e), "error")
        return redirect(url_for("presets_page"))
    flash(f"Preset \"{preset['PresetName']}\" created.", "ok")
    return redirect(url_for("preset_edit", pid=pid))


@app.route("/presets/<pid>", methods=["GET", "POST"])
def preset_edit(pid):
    if not presets.exists(pid):
        abort(404)
    preset, error, json_text, new = presets.load(pid), None, None, None
    caps, remote = presets.editor_capabilities(encodes_here())   # the HandBrakeCLI that encodes
    if request.method == "POST":
        try:
            if request.form.get("mode") == "json":
                json_text = request.form.get("json", "")
                try:
                    new = json.loads(json_text)
                except ValueError as e:
                    raise presets.PresetError(f"Invalid JSON: {e}")
                if isinstance(new, dict) and isinstance(new.get("PresetList"), list) and new["PresetList"]:
                    new = new["PresetList"][0]         # a whole export file was pasted
            else:
                new = presets.apply_form(preset, request.form, caps)
            presets.check(new)
            problem = presets.verify(new, caps, remote)
            if problem:
                raise presets.PresetError(problem)
            presets.save(pid, new)
            flash(f"Preset \"{new['PresetName']}\" saved.", "ok")
            return redirect(url_for("preset_edit", pid=pid))
        except (presets.PresetError, ValueError, TypeError) as e:
            error = str(e)
    usage, default = preset_usage()
    shown = new if error and new and request.form.get("mode") != "json" else preset   # keep the user's input on errors
    tabs, tracks, track_template = presets.editor(shown, caps)
    return render_template(
        "preset_edit.html", pid=pid, p=preset, caps=caps, error=error, tabs=tabs, tracks=tracks, remote=remote, tab_icons=TAB_ICONS,
        compat=presets.compatibility(shown, caps, remote, presets.file_format(pid)),
        track_template=track_template, json_text=json_text or json.dumps(preset, indent=2, ensure_ascii=False),
        shares=usage.get(pid, []), is_default=pid == default)


def encodes_here():
    """True if this machine's HandBrakeCLI is the one that encodes: always natively, in
    Docker only with WORKER_ENABLED (the environment variable wins, like for the worker)."""
    if not Path("/.dockerenv").exists():
        return True
    value = os.environ.get("WORKER_ENABLED") or raw_config().get("WORKER_ENABLED") or "1"
    return value.strip() == "1"


@app.route("/presets/<pid>/export")
def preset_export(pid):
    if not presets.exists(pid):
        abort(404)
    name = re.sub(r"[^\w.-]+", "_", presets.name_of(pid))
    return Response(presets.export_bytes(pid), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.json"'})


@app.route("/presets/<pid>/default", methods=["POST"])
def preset_default(pid):
    if not presets.exists(pid):
        abort(404)
    enc.write_config({"DEFAULT_PRESET": pid})
    flash(f"\"{presets.name_of(pid)}\" is now the default preset.", "ok")
    return redirect(url_for("presets_page"))


@app.route("/presets/<pid>/delete", methods=["POST"])
def preset_delete(pid):
    if not presets.exists(pid):
        abort(404)
    usage, default = preset_usage()
    name = presets.name_of(pid)
    if pid == default:
        flash(f"\"{name}\" is the default preset. Choose another default first.", "error")
    elif usage.get(pid):
        flash(f"\"{name}\" is used by {', '.join(usage[pid])}. Change their preset first.", "error")
    elif any(p == pid for _, p in enc.queued_entries()):
        flash(f"\"{name}\" is used by files in the queue. Wait until they're encoded.", "error")
    else:
        presets.delete(pid)
        flash(f"Preset \"{name}\" deleted.", "ok")
    return redirect(url_for("presets_page"))


@app.route("/settings")
def settings_index():
    return redirect(url_for("settings", section=next(iter(SETTINGS))))


@app.route("/settings/<section>", methods=["GET", "POST"])
def settings(section):
    if section not in SETTINGS:
        abort(404)
    title, fields = SETTINGS[section]
    cfg = {**SETTING_DEFAULTS, **raw_config()}
    if section == "library":
        fields = library_fields(cfg)
    errors = {}
    if request.method == "POST":
        updates, removed = {}, []
        valid_presets = {pid for pid, _ in preset_choices()}
        for key, label, typ, _ in fields:
            if typ == "heading":
                continue
            if typ == "bool":
                updates[key] = "1" if request.form.get(key) else "0"
                continue
            if typ == "event_groups":
                picked = set(request.form.getlist(key))
                updates[key] = " ".join(g for g in EVENT_GROUPS if g in picked) or "none"
                continue
            val = request.form.get(key, "").strip()
            if FORBIDDEN_RE.search(val):
                errors[key] = 'Not allowed: " ` \\ line breaks and $ (except $HOME).'
            elif typ in ("preset", "share_preset") and val and val not in valid_presets:
                errors[key] = "Unknown preset."
            elif typ == "preset" and not val:
                errors[key] = "Choose a preset."
            elif key == "ENCODED_DIR" and not val:
                errors[key] = "This folder is required."
            elif typ == "date_format" and val not in DATE_FORMATS:
                errors[key] = "Choose one of the listed formats."
            elif typ == "number" and not re.fullmatch(r"\d{1,6}", val or "0"):
                errors[key] = "Enter a whole number (0 = off)."
            if typ == "number":
                val = str(int(val)) if re.fullmatch(r"\d{1,6}", val) else (val or "0")
            if not key.startswith("SHARE_new"):
                updates[key] = val
        if not errors:
            enc.write_config(updates)
            msg = f"{title}: saved."
            if removed:
                msg += (f" Removed {', '.join(removed)}. Its files disappear from the list "
                        "with the next scan.")
            flash(msg, "ok")
            return redirect(url_for("settings", section=section))
        cfg.update({k: v for k, v in updates.items() if v is not None})

    return render_template(
        "settings.html", section=section, title=title, fields=fields, cfg=cfg, errors=errors,
        checks={k: path_checks(cfg.get(k) or "") for k, _, t, _ in fields if t in ("paths", "path")},
        preset_list=preset_choices(), default_preset=presets.name_of(cfg.get("DEFAULT_PRESET", "")),
        date_formats=DATE_FORMATS, event_groups=EVENT_GROUPS, share_info=share_info() if section == "library" else {},
        shares=share_list(cfg) if section == "library" else [], open_share=request.args.get("open", ""),
        next_share=enc.next_share_number() if section == "library" else None,
        plex_status=plex_status(cfg) if section == "plex" else None,
        config_file=enc.CONFIG_FILE, config_exists=enc.CONFIG_FILE.exists(),
        env_overrides={k: os.environ[k] for k, _, _, _ in fields if k in ENV_OVERRIDES and k in os.environ})


def main():
    ap = argparse.ArgumentParser(description="Handlebreaks web UI")
    ap.add_argument("--host", default=os.environ.get("WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "7979")))
    args = ap.parse_args()
    print(f"Handlebreaks web UI: http://{args.host}:{args.port}", file=sys.stderr)
    threading.Thread(target=auto_scan_loop, daemon=True).start()
    threading.Thread(target=replace_sync_loop, daemon=True).start()
    threading.Thread(target=originals_purge_loop, daemon=True).start()
    threading.Thread(target=event_loop, daemon=True).start()
    try:
        from waitress import serve
    except ImportError:
        print("waitress is not installed, starting the Flask development server.", file=sys.stderr)
        app.run(host=args.host, port=args.port)
        return
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
