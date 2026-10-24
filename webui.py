#!/usr/bin/env python3
"""
Web UI for the encoding control
===============================

Pages
-----
Files       All scanned videos as a folder tree with codec, resolution, bitrate,
            size and status. Filters by text, codec and status. Files can be
            selected and queued for encoding.
Status      Current encode, queue, latest results, start a scan.
Stats       Distribution of codecs, bitrates, file sizes, resolutions and statuses.
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
import math
import os
import re
import secrets
import sys
import threading
from collections import Counter
from contextlib import closing
from pathlib import Path

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

import encoding as enc
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


def library_fields(cfg):
    """Settings fields of the library section: name, folder and preset of every share,
    an empty group to add a share, and the output folder."""
    fields = []
    for sh in enc.shares(enc.parse_config(enc.CONFIG_FILE)) if enc.CONFIG_FILE.exists() else []:
        n = sh["n"]
        fields += [
            (f"SHARE_{n}", f"Share {n}", "heading", f"share{n}"),
            (f"SHARE_{n}_NAME", "Name", "text", "Shown in the web UI, the notes and the CLI."),
            (f"SHARE_{n}", "Folder", "paths", "Read only, never modified. Clear it to remove the share."),
            (f"SHARE_{n}_PRESET", "Preset", "share_preset", ""),
        ]
    fields += [
        ("SHARE_new", f"Add a share (saved as share {enc.next_share_number()})", "heading", ""),
        ("SHARE_new_NAME", "Name", "text", "Optional. Defaults to the folder name."),
        ("SHARE_new", "Folder", "paths_new", "Optional."),
        ("SHARE_new_PRESET", "Preset", "share_preset", ""),
        ("ENCODED", "Output", "heading", ""),
        ("ENCODED_DIR", "Output folder", "paths", "Finished files land here and wait for your review."),
    ]
    return fields


def path_checks(value):
    """For each path of a ":" list: (path, exists)."""
    return [(p, os.path.exists(p)) for p in enc.path_list(os.path.expandvars(value))]


def share_info():
    """Key prefix -> {"files": number of files in the inventory, "scanned": last scan time}."""
    with closing(db()) as con:
        counts = dict(con.execute("SELECT substr(key, 1, instr(key, '/') - 1), COUNT(*) FROM files GROUP BY 1"))
    scans = enc.share_scans()
    return {p: {"files": counts.get(p, 0), "scanned": scans.get(p)} for p in enc.ROOT_CANDIDATES}


def preset_choices():
    """[(id, name)] of all presets, for dropdowns."""
    return [(pid, p.get("PresetName", pid)) for pid, p in presets.all_presets()]


# ─── Background scan ──────────────────────────────────────────────────────────

SCAN = {"running": False, "started": None, "finished": None, "error": None, "summary": None, "label": None}
SCAN_GUARD = threading.Lock()


def run_scan(only=None, force=False):
    try:
        enc.apply_config(enc.load_config())
        s = enc.scan(enc.load_state(), only=only, force=force)
        SCAN.update(error=None, summary=f"{SCAN['label']}: {s['videos_total']:,} videos in the library, "
                                        f"{s['already_h265']:,} already optimized, {len(s['offer']):,} offered")
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
AUTO_OPEN = 300                                 # with a filter, open folders with up to this many matches


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


def file_filters(src):
    """Filter values from a request (query string or form). codec is a list: codec
    names, "unknown" (not probed) and the groups in CODEC_GROUPS can be combined."""
    filters = {k: src.get(k, "").strip() for k in ("q", "status") if src.get(k, "").strip()}
    codecs = [c.strip() for c in src.getlist("codec") if c.strip()]
    if codecs:
        filters["codec"] = codecs
    return filters


def filter_sql(filters, path=""):
    """WHERE clause and parameters for the filters, limited to the folder `path`."""
    where, params = [], []
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


def tree_level(path, filters, queued, offset=0):
    """Direct subfolders and files of `path` ("" = top level) that match the filters.
    Returns (folders, files of the requested page, number of files in total)."""
    clause, params = filter_sql(filters, path)
    plen = len(path) + 1 if path else 0
    folders, keys = {}, []
    with closing(db()) as con:
        for key, size in con.execute(f"SELECT key, size FROM files{clause}", params):
            head, sep, _ = key[plen:].partition("/")
            if not sep:
                keys.append(key)
                continue
            f = folders.get(head)
            if f is None:
                f = folders[head] = {"name": head, "path": f"{path}/{head}" if path else head,
                                     "count": 0, "size": 0, "selectable": 0}
            f["count"] += 1
            f["size"] += size or 0
            f["selectable"] += key not in queued
        keys.sort(key=natural)
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
    files = [dict(rows[k], name=k[plen:]) for k in page if k in rows]
    return sorted(folders.values(), key=lambda f: natural(f["name"])), files, len(keys)


def level_context(path, filters, queued, offset=0):
    folders, files, total = tree_level(path, filters, queued, offset)
    nxt = offset + PAGE_SIZE
    more = url_for("files_folder", path=path, offset=nxt, rows=1, **filters) if nxt < total else None
    return {"folders": folders, "files": files, "total_files": total, "shown_files": min(nxt, total),
            "more_url": more, "filters": filters, "queued": queued, "auto_open": AUTO_OPEN,
            "status_labels": enc.STATUS_LABELS}


@app.route("/files")
def files():
    filters = file_filters(request.args)
    empty = render_template("files.html", total=0, filters=filters)
    if g.config_error:
        return empty
    with closing(db()) as con:
        total, total_size, opt_size = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(CASE WHEN status = 'h265' THEN size END), 0) "
            "FROM files").fetchone()
        if not total:
            return empty
        codecs = con.execute(f"SELECT COALESCE(video_codec, '{UNKNOWN_CODEC}'), COUNT(*) FROM files "
                             "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        statuses = dict(con.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        clause, params = filter_sql(filters)
        shown = con.execute(f"SELECT COUNT(*) FROM files{clause}", params).fetchone()[0]
    queued = set(enc.queued_keys())
    return render_template(
        "files.html", total=total, total_size=total_size, shown=shown,
        optimized_share=opt_size / total_size * 100 if total_size else 0, codecs=codecs,
        statuses=statuses, q=filters.get("q", ""), codec=filters.get("codec", []),
        status=filters.get("status", ""), filtered=bool(filters),
        codec_groups=[(k, label, sum(n for c, n in codecs if codec_class(c) == k.split(":")[1]))
                      for k, label in CODEC_GROUPS.items()],
        codec_summary=codec_summary(filters.get("codec", [])),
        last_scan=enc.load_state()["last_scan"], ffprobe=bool(enc.FFPROBE), preset_list=preset_choices(),
        unscanned=[enc.SHARE_NAMES[p] for p, i in share_info().items() if not i["files"]],
        **level_context("", filters, queued))


@app.route("/files/folder")
def files_folder():
    """Content of one folder for the tree: subfolders and the first page of files,
    or with rows=1 only the next page of file rows ("Show more")."""
    if g.config_error:
        abort(409, g.config_error)
    path = request.args.get("path", "").strip("/")
    offset = max(int(request.args.get("offset", 0) or 0), 0)
    ctx = level_context(path, file_filters(request.args), set(enc.queued_keys()), offset)
    if request.args.get("rows"):
        resp = app.make_response(render_template("_file_rows.html", **ctx))
        resp.headers["X-Next"] = ctx["more_url"] or ""
        return resp
    return render_template("_level.html", **ctx)


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


@app.route("/status")
def status():
    cur = enc.current_progress()
    entries = enc.queued_entries()
    keys = [k for k, _ in entries]
    if cur and cur["key"] in keys:
        entries = entries[keys.index(cur["key"]) + 1:]
    queue = [(k, presets.name_of(pid) if pid else "") for k, pid in entries]
    return render_template("status.html", cur=cur, queue=queue, results=enc.recent_results(20),
                           short=enc.short, last_scan=enc.load_state()["last_scan"])


@app.route("/scan", methods=["POST"])
def start_scan():
    """Scan all shares (new or changed files only), or with share=shareN one share:
    an initial scan of a new share, or with share=shareN:force a full rescan that
    re-reads every file of the share with ffprobe."""
    if g.config_error:
        flash(g.config_error, "error")
        return redirect(url_for("settings", section="library"))
    prefix, _, mode = request.form.get("share", "").partition(":")
    if prefix and prefix not in enc.ROOT_CANDIDATES:
        abort(400, "Unknown share.")
    only, force = ([prefix] if prefix else None), mode == "force"
    name = enc.SHARE_NAMES.get(prefix, prefix)
    label = f"{'Full rescan' if force else 'Scan'} of {name}" if prefix else "Scan of all shares"
    with SCAN_GUARD:
        if SCAN["running"]:
            flash("A scan is already running.", "error")
        else:
            SCAN.update(running=True, started=enc.now_str(), finished=None, error=None, summary=None, label=label)
            threading.Thread(target=run_scan, args=(only, force), daemon=True).start()
            flash(f"{label} started. Files are read with ffprobe, which can take a while"
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


def resolution_class(w, h):
    if not w or not h:
        return "Unknown"
    if w >= 3200 or h >= 1800:
        return "2160p (4K)"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return "SD"


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


@app.route("/stats")
def stats():
    rows = list(enc.load_files().values()) if not g.config_error else []
    if not rows:
        return render_template("stats.html", charts=[], tiles=None)

    by_codec_n, by_codec_size = Counter(), Counter()
    bitrate, size, res = {}, {}, {}
    sections, status = {}, Counter()
    no_bitrate = 0
    for r in rows:
        cls = codec_class(r["video_codec"])
        codec = r["video_codec"] or UNKNOWN_CODEC
        by_codec_n[codec] += 1
        by_codec_size[codec] += r["size"] or 0
        if r["bitrate"]:
            mbit = r["bitrate"] / 1e6
            b = next(lbl for lo, hi, lbl in BITRATE_BINS if lo <= mbit < hi)
            bitrate.setdefault(b, Counter())[cls] += 1
        else:
            no_bitrate += 1
        gb = (r["size"] or 0) / GB
        sb = next(lbl for lo, hi, lbl in SIZE_BINS if lo <= gb < hi)
        size.setdefault(sb, Counter())[cls] += 1
        res.setdefault(resolution_class(r["width"], r["height"]), Counter())[cls] += 1
        sections.setdefault(enc.SHARE_NAMES.get(r["key"].split("/", 1)[0], r["key"].split("/", 1)[0]),
                            Counter())[cls] += r["size"] or 0
        status[r["status"]] += 1

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

    total_size = sum(r["size"] or 0 for r in rows)
    good = [r for r in rows if codec_class(r["video_codec"]) == "good"]
    other = [r for r in rows if codec_class(r["video_codec"]) == "other"]
    good_size = sum(r["size"] or 0 for r in good)

    def median_bitrate(rs):
        values = sorted(r["bitrate"] for r in rs if r["bitrate"])
        return values[len(values) // 2] if values else None

    tiles = {
        "videos": len(rows), "total_size": total_size,
        "good_share": good_size / total_size * 100 if total_size else 0,
        "good_count": len(good), "other_count": len(other),
        "other_size": sum(r["size"] or 0 for r in other),
        "median_good": median_bitrate(good), "median_other": median_bitrate(other),
    }
    return render_template("stats.html", charts=charts, tiles=tiles, last_scan=enc.load_state()["last_scan"])


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
    preset, error, json_text = presets.load(pid), None, None
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
                new = presets.apply_form(preset, request.form)
            presets.check(new)
            problem = presets.verify(new)
            if problem:
                raise presets.PresetError(problem)
            presets.save(pid, new)
            flash(f"Preset \"{new['PresetName']}\" saved.", "ok")
            return redirect(url_for("preset_edit", pid=pid))
        except (presets.PresetError, ValueError, TypeError) as e:
            error = str(e)
    usage, default = preset_usage()
    caps = presets.capabilities()
    audio = (preset.get("AudioList") or [{}])[0]
    return render_template(
        "preset_edit.html", pid=pid, p=preset, audio=audio, caps=caps, error=error,
        json_text=json_text or json.dumps(preset, indent=2, ensure_ascii=False),
        resolution=presets.resolution_of(preset), resolutions=presets.RESOLUTIONS,
        framerates=presets.FRAMERATES, track_behaviors=presets.TRACK_BEHAVIORS,
        burn_behaviors=presets.BURN_BEHAVIORS, shares=usage.get(pid, []), is_default=pid == default,
        aliases=presets.AUDIO_ALIASES)


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
    cfg = raw_config()
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
            val = request.form.get(key, "").strip()
            if FORBIDDEN_RE.search(val):
                errors[key] = 'Not allowed: " ` \\ line breaks and $ (except $HOME).'
            elif typ in ("preset", "share_preset") and val and val not in valid_presets:
                errors[key] = "Unknown preset."
            elif typ == "preset" and not val:
                errors[key] = "Choose a preset."
            elif key == "ENCODED_DIR" and not val:
                errors[key] = "This folder is required."
            if not key.startswith("SHARE_new"):
                updates[key] = val
        # Shares whose folder was cleared are removed (commented out, number stays reserved)
        for key in [k for k in updates if re.fullmatch(r"SHARE_\d+", k)]:
            if not updates[key]:
                removed.append(updates.get(f"{key}_NAME") or key)
                for k in (key, f"{key}_NAME", f"{key}_PRESET"):
                    updates[k] = None
        if section == "library":
            new_folder = request.form.get("SHARE_new", "").strip()
            if new_folder and not FORBIDDEN_RE.search(new_folder):
                n = enc.next_share_number()
                updates[f"SHARE_{n}"] = new_folder
                updates[f"SHARE_{n}_NAME"] = (request.form.get("SHARE_new_NAME", "").strip()
                                             or Path(enc.path_list(os.path.expandvars(new_folder))[0]).name)
                updates[f"SHARE_{n}_PRESET"] = request.form.get("SHARE_new_PRESET", "")
            remaining = [k for k, v in updates.items() if re.fullmatch(r"SHARE_\d+", k) and v]
            if not remaining:
                errors["SHARE_new"] = "At least one share is needed."
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
        share_info=share_info() if section == "library" else {},
        config_file=enc.CONFIG_FILE, config_exists=enc.CONFIG_FILE.exists(),
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
