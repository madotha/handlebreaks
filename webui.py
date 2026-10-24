#!/usr/bin/env python3
"""
Web UI for the encoding control
===============================

Pages
-----
Files       All scanned videos as a folder tree with codec, resolution, bitrate,
            size and status. Filters by text, codec and status.
Status      Current encode, queue, latest results, start a scan.
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
import json
import os
import re
import secrets
import sys
import threading
from collections import Counter

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

import encoding as enc

app = Flask(__name__)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)


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

# section -> (title, [(key, label, type, hint)])
SETTINGS = {
    "library": ("Library", [
        ("MOVIES_DIR", "Movies", "paths", "Folder with the movies. Read only, never modified."),
        ("SHOWS_DIR", "TV shows", "paths", "Folder with the TV shows. Read only, never modified."),
        ("ENCODED_DIR", "Output folder", "paths", "Finished files land here and wait for your review."),
    ]),
    "encoding": ("Encoding", [
        ("PRESET_FILE", "Preset file", "path", "Exported from the HandBrake app. Empty = preset.json in the program folder."),
        ("PRESET_NAME", "Preset name", "preset", "Internal name (PresetName) of the preset in the file."),
    ]),
    "notes": ("Obsidian", [
        ("NOTE_DIR", "Notes folder", "paths", "For \"Encoding To-Do.md\" and \"Encoding Status.md\". Empty = no notes."),
        ("STATUS_NOTE_ENABLED", "Update the status note every minute while encoding", "bool", ""),
    ]),
    "worker": ("Worker", [
        ("WORKER_ENABLED", "Encode inside the Docker container", "bool",
         "Docker only. Turn off when the Mac encodes natively with launchd and encode.sh, "
         "and the container only runs the web UI and scans."),
    ]),
}

# Settings overridden by an environment variable of the same name (docker-entrypoint.sh)
ENV_OVERRIDES = {"WORKER_ENABLED"}

# Allowed: everything except characters that could run code in config.env (read by zsh)
# or end the quotes. $ only as $HOME or ${HOME}.
FORBIDDEN_RE = re.compile(r'["`\\\n\r]|\$(?!HOME\b|\{HOME\})')


def raw_config():
    """Values as written in config.env ($HOME not expanded), otherwise from the template."""
    src = enc.CONFIG_FILE if enc.CONFIG_FILE.exists() else enc.EXAMPLE_CONFIG
    return enc.parse_config(src, expand=False)


def write_config(updates):
    """Replace values in config.env, keeping comments and order."""
    src = enc.CONFIG_FILE if enc.CONFIG_FILE.exists() else enc.EXAMPLE_CONFIG
    lines = src.read_text(encoding="utf-8").splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in updates:
            lines[i] = f'{m.group(1)}="{updates[m.group(1)]}"'
            done.add(m.group(1))
    lines += [f'{k}="{v}"' for k, v in updates.items() if k not in done]
    enc.DATA_DIR.mkdir(parents=True, exist_ok=True)
    enc.write_text_atomic(enc.CONFIG_FILE, "\n".join(lines) + "\n")


def path_checks(value):
    """For each path of a ":" list: (path, exists)."""
    return [(p, os.path.exists(p)) for p in enc.path_list(os.path.expandvars(value))]


def preset_names(cfg):
    path = os.path.expandvars(cfg.get("PRESET_FILE") or "") or str(enc.BASE / "preset.json")
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        return [p.get("PresetName") for p in data.get("PresetList", []) if p.get("PresetName")]
    except (OSError, ValueError):
        return []

# ─── Background scan ──────────────────────────────────────────────────────────

SCAN = {"running": False, "started": None, "finished": None, "error": None, "summary": None}
SCAN_GUARD = threading.Lock()


def run_scan():
    try:
        enc.apply_config(enc.load_config())
        s = enc.scan(enc.load_state())
        SCAN.update(error=None, summary=f"{s['videos_total']} videos, {s['already_h265']} already H.265, "
                                        f"{len(s['offer'])} offered")
    except SystemExit as e:
        SCAN.update(error=str(e), summary=None)
    except Exception as e:                      # show errors in the UI instead of swallowing them
        SCAN.update(error=f"{type(e).__name__}: {e}", summary=None)
    finally:
        SCAN.update(running=False, finished=enc.now_str())

# ─── Requests ─────────────────────────────────────────────────────────────────


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals.update(csrf_token=csrf_token, settings_sections=SETTINGS, scan_state=SCAN)


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


@app.context_processor
def inject():
    return {"config_error": g.get("config_error")}


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


def new_node():
    return {"dirs": {}, "files": [], "count": 0, "size": 0}


@app.route("/files")
def files():
    rows = list(enc.load_files().values()) if not g.config_error else []
    q = request.args.get("q", "").strip().lower()
    codec = request.args.get("codec", "")
    status = request.args.get("status", "")

    codecs = Counter((r["video_codec"] or "unbekannt") for r in rows)
    statuses = Counter(r["status"] for r in rows)
    total_size = sum(r["size"] or 0 for r in rows)
    hevc_size = sum(r["size"] or 0 for r in rows if r["status"] == "h265")

    shown = [r for r in rows
             if (not q or q in r["key"].lower())
             and (not codec or (r["video_codec"] or "unbekannt") == codec)
             and (not status or r["status"] == status)]

    tree = new_node()
    for r in sorted(shown, key=lambda r: r["key"].lower()):
        node = tree
        for part in r["key"].split("/")[:-1]:
            node = node["dirs"].setdefault(part, new_node())
            node["count"] += 1
            node["size"] += r["size"] or 0
        node["files"].append(dict(r, name=r["key"].rsplit("/", 1)[-1]))

    return render_template(
        "files.html", tree=tree, shown=len(shown), total=len(rows), total_size=total_size,
        hevc_share=(hevc_size / total_size * 100) if total_size else 0, codecs=codecs.most_common(),
        statuses=statuses, status_labels=enc.STATUS_LABELS, q=q, codec=codec, status=status,
        filtered=bool(q or codec or status), last_scan=enc.load_state()["last_scan"] if rows else None,
        ffprobe=bool(enc.FFPROBE))


@app.route("/status")
def status():
    cur = enc.current_progress()
    queue = enc.queued_keys()
    if cur and cur["key"] in queue:
        queue = queue[queue.index(cur["key"]) + 1:]
    return render_template("status.html", cur=cur, queue=queue, results=enc.recent_results(20),
                           short=enc.short, last_scan=enc.load_state()["last_scan"])


@app.route("/scan", methods=["POST"])
def start_scan():
    if g.config_error:
        flash(g.config_error, "error")
        return redirect(url_for("settings", section="library"))
    with SCAN_GUARD:
        if SCAN["running"]:
            flash("A scan is already running.", "error")
        else:
            SCAN.update(running=True, started=enc.now_str(), finished=None, error=None, summary=None)
            threading.Thread(target=run_scan, daemon=True).start()
            flash("Scan started. New files are read with ffprobe, which can take a while.", "ok")
    nxt = request.form.get("next", "")             # only paths on this site, no external addresses
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("status"))


@app.route("/settings")
def settings_index():
    return redirect(url_for("settings", section=next(iter(SETTINGS))))


@app.route("/settings/<section>", methods=["GET", "POST"])
def settings(section):
    if section not in SETTINGS:
        abort(404)
    title, fields = SETTINGS[section]
    cfg = raw_config()
    errors = {}
    if request.method == "POST":
        updates = {}
        for key, _, typ, _ in fields:
            if typ == "bool":
                updates[key] = "1" if request.form.get(key) else "0"
                continue
            val = request.form.get(key, "").strip()
            if FORBIDDEN_RE.search(val):
                errors[key] = 'Not allowed: " ` \\ line breaks and $ (except $HOME).'
            updates[key] = val
        if not errors:
            write_config(updates)
            flash(f"{title}: saved.", "ok")
            return redirect(url_for("settings", section=section))
        cfg.update(updates)

    return render_template(
        "settings.html", section=section, title=title, fields=fields, cfg=cfg, errors=errors,
        checks={k: path_checks(cfg.get(k, "")) for k, _, t, _ in fields if t in ("paths", "path")},
        presets=preset_names(cfg), config_file=enc.CONFIG_FILE, config_exists=enc.CONFIG_FILE.exists(),
        env_overrides={k: os.environ[k] for k, _, _, _ in fields if k in ENV_OVERRIDES and k in os.environ})


def main():
    ap = argparse.ArgumentParser(description="Web UI of the encoding control")
    ap.add_argument("--host", default=os.environ.get("WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "7979")))
    args = ap.parse_args()
    print(f"Web UI: http://{args.host}:{args.port}", file=sys.stderr)
    try:
        from waitress import serve
    except ImportError:
        print("waitress is not installed, starting the Flask development server.", file=sys.stderr)
        app.run(host=args.host, port=args.port)
        return
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
