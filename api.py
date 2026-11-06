"""
The Handlebreaks API, version 1: reading (phase 1), writing (phase 2) and the opt-in scopes that
change the library (phase 3). Specification: docs/api-v1-spec.md and the
wiki page "API"; the OpenAPI description is generated from the route registry below
(GET /api/v1/openapi.json).

Every request needs `Authorization: Bearer hb_<id>_<secret>` (apitokens.py); the web UI's
session cookie is never accepted here, so no CSRF token is involved. Each route is declared with
@route(method, path, scope, summary, params, response); the scope check, the reference page in
Settings → API and `python3 webui.py --check-api` all read that registry.

This module doesn't import webui (it may run as __main__); webui calls init(module) and the
routes reach its helpers through `ui`.
"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, g, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.datastructures import MultiDict

import apitokens
import encoding as enc
import presets

API_VERSION = "1.0"
bp = Blueprint("api", __name__, url_prefix="/api/v1")
ui = None                                       # the webui module, set by init()

READ_PER_MINUTE, WRITE_PER_MINUTE = 120, 20
FAILS_FREE, FAILS_WINDOW, FAILS_MAX_WAIT = 10, 15 * 60, 15 * 60
LIMIT_DEFAULT, LIMIT_MAX = 100, 500
CURSOR_TTL = 600
FAIL_LOG_RE = re.compile(r"fail-(\d{8}-\d{6})\.log")


class APIError(Exception):
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


# ─── Registry ─────────────────────────────────────────────────────────────────
ROUTES = []                                     # [{"method", "path", "endpoint", "scope", "summary", "params", "response"}]


def route(method, path, scope, summary, params=(), response=None):
    """Declare an API route. scope None = any valid token. params: [(name, type, description)];
    response: an example answer (documentation and OpenAPI)."""
    def deco(fn):
        endpoint = fn.__name__
        flask_path = re.sub(r"\{(\w+)\}", r"<\1>", path)
        bp.add_url_rule(flask_path, endpoint, fn, methods=[method])
        ROUTES.append({"method": method, "path": path, "endpoint": "api." + endpoint, "scope": scope,
                       "summary": summary, "params": list(params), "response": response})
        return fn
    return deco


def route_of(endpoint):
    return next((r for r in ROUTES if r["endpoint"] == endpoint), None)


def problems():
    """Routes without scope declaration, summary or example answer (python3 webui.py --check-api)."""
    out = []
    for r in ROUTES:
        if r["scope"] is not None and r["scope"] not in apitokens.SCOPES:
            out.append(f"{r['method']} {r['path']}: unknown scope {r['scope']}")
        if not r["summary"]:
            out.append(f"{r['method']} {r['path']}: no summary")
        if r["response"] is None:
            out.append(f"{r['method']} {r['path']}: no example answer")
    return out


# ─── Requests: authentication, limits, errors ─────────────────────────────────
_LOCK = threading.Lock()
_FAILS = {}                                     # address -> [count, first, blocked until]
_CALLS = {}                                     # (token id, read|write) -> deque of times


def setting(key, default=""):
    if key in os.environ:
        return os.environ[key].strip()
    value = enc.CONFIG.get(key)
    return (value if value is not None else default).strip()


def client_address():
    """The TCP peer, or with API_TRUSTED_PROXIES the rightmost untrusted X-Forwarded-For address."""
    peer = request.remote_addr or ""
    trusted = []
    for part in setting("API_TRUSTED_PROXIES").replace(",", " ").split():
        try:
            trusted.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue

    def is_trusted(addr):
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in n for n in trusted)
    if not trusted or not is_trusted(peer):
        return peer
    chain = [a.strip() for a in request.headers.get("X-Forwarded-For", "").split(",") if a.strip()]
    for addr in reversed(chain):
        if not is_trusted(addr):
            return addr
    return peer


def _blocked(addr):
    with _LOCK:
        e = _FAILS.get(addr)
        if not e:
            return 0
        now = time.time()
        if now - e[1] > FAILS_WINDOW and now >= e[2]:
            del _FAILS[addr]
            return 0
        return max(0, int(e[2] - now + 0.999))


def _failed(addr):
    with _LOCK:
        now = time.time()
        e = _FAILS.get(addr)
        if not e or (now - e[1] > FAILS_WINDOW and now >= e[2]):
            e = _FAILS[addr] = [0, now, 0]
        e[0] += 1
        if e[0] >= FAILS_FREE:
            e[2] = now + min(FAILS_MAX_WAIT, 2 ** (e[0] - FAILS_FREE) * 15)
        if len(_FAILS) > 10000:
            for k in [k for k, v in _FAILS.items() if now - v[1] > FAILS_WINDOW]:
                del _FAILS[k]


def _rate(tid, kind):
    """(allowed, limit, remaining, reset seconds) for one more call of this token."""
    limit = READ_PER_MINUTE if kind == "read" else WRITE_PER_MINUTE
    now = time.time()
    with _LOCK:
        q = _CALLS.setdefault((tid, kind), deque())
        while q and now - q[0] >= 60:
            q.popleft()
        reset = int(60 - (now - q[0])) + 1 if q else 60
        if len(q) >= limit:
            return False, limit, 0, reset
        q.append(now)
        return True, limit, limit - len(q), reset


@bp.before_request
def authenticate():
    g.request_id = secrets.token_hex(8)
    if setting("API_ENABLED", "1") == "0":
        raise APIError(404, "not_found", "Not found.")
    addr = g.api_address = client_address()
    wait = _blocked(addr)
    if wait:
        raise APIError(429, "rate_limited", "Too many failed attempts from this address.", retry_after=wait)
    token = apitokens.verify(request.headers.get("Authorization"))
    g.token = token
    if not token:
        _failed(addr)
        raise APIError(401, "unauthenticated", "A valid API token is needed (Authorization: Bearer hb_…).")
    if not apitokens.address_allowed(token, addr):
        raise APIError(403, "address_not_allowed", "This token can't be used from this address.")
    spec = route_of(request.endpoint)
    kind = "write" if request.method not in ("GET", "HEAD") else "read"
    ok, limit, remaining, reset = _rate(token["id"], kind)
    g.rate = (limit, remaining, reset)
    if not ok:
        raise APIError(429, "rate_limited", f"At most {limit} {kind} requests per minute per token.", retry_after=reset)
    if spec and spec["scope"] and spec["scope"] not in token["scopes"]:
        raise APIError(403, "missing_scope", f"This token lacks the scope {spec['scope']}.", scope=spec["scope"])
    g.token = token
    apitokens.touch(token["id"], addr)


@bp.after_request
def headers(resp):
    log_request(resp.status_code)
    resp.headers["X-Request-Id"] = g.get("request_id", "")
    resp.headers["Cache-Control"] = "no-store"
    if g.get("rate"):
        limit, remaining, reset = g.rate
        resp.headers.update({"RateLimit-Limit": str(limit), "RateLimit-Remaining": str(remaining),
                             "RateLimit-Reset": str(reset)})
    if request.method == "GET" and resp.status_code == 200 and resp.mimetype == "application/json":
        etag = '"' + hashlib.sha256(resp.get_data()).hexdigest()[:32] + '"'
        resp.headers["ETag"] = etag
        if etag in [t.strip() for t in request.headers.get("If-None-Match", "").split(",")]:
            resp = Response(status=304, headers={k: v for k, v in resp.headers.items()
                                                 if k.lower() not in ("content-length", "content-type")})
    return resp


def log_request(status):
    """One line of the activity page per request (Settings → API → Activity)."""
    if g.get("logged"):
        return
    g.logged = True
    t = g.get("token")
    token_id = t["id"] if t else None
    if not t:                                   # name the id a failed request claimed, if it had one
        m = re.search(r"hb_([a-z2-7]{8})_", request.headers.get("Authorization", ""))
        token_id = m.group(1) if m else None
    try:
        enc.log_api(token_id, t["name"] if t else None, g.get("api_address") or request.remote_addr,
                    request.method, request.path, status, g.get("action"))
    except Exception as e:                      # the log must never break a request
        print(f"API log failed: {e}", file=sys.stderr)


def error_response(status, code, message, **details):
    body = {"error": {"code": code, "message": message, "details": {k: v for k, v in details.items() if k != "retry_after"}}}
    resp = Response(json.dumps(body), status=status, mimetype="application/json")
    resp.headers["X-Request-Id"] = g.get("request_id", "")
    resp.headers["Cache-Control"] = "no-store"
    if status == 401:
        resp.headers["WWW-Authenticate"] = 'Bearer realm="Handlebreaks"'
    if details.get("retry_after"):
        resp.headers["Retry-After"] = str(details["retry_after"])
    return resp


@bp.errorhandler(APIError)
def api_error(e):
    resp = error_response(e.status, e.code, e.message, **e.details)
    log_request(e.status)
    return resp


def init(webui_module):
    """Register the blueprint on the web UI's app. Errors of /api/ URLs are always JSON."""
    global ui
    ui = webui_module
    app = ui.app
    app.register_blueprint(bp)

    def json_errors(e):
        if not request.path.startswith(bp.url_prefix + "/") and request.path != bp.url_prefix:
            return None
        if isinstance(e, APIError):
            return api_error(e)
        status = getattr(e, "code", 500) or 500
        if status >= 500:
            print(f"API error {g.get('request_id', '')}: {e!r}", file=sys.stderr, flush=True)
        codes = {404: ("not_found", "Not found."), 405: ("method_not_allowed", "Method not allowed."),
                 400: ("invalid_request", "Bad request."), 413: ("too_large", "Request too large.")}
        code, message = codes.get(status, ("internal_error", "Internal error."))
        return error_response(status if status in codes else 500, code, message)

    for status in (400, 404, 405, 413, 500):
        prev = app.error_handler_spec.get(None, {}).get(status, {})

        def handler(e, _prev=dict(prev)):
            resp = json_errors(e)
            if resp is not None:
                return resp
            for cls, fn in _prev.items():
                if isinstance(e, cls):
                    return fn(e)
            return e
        app.register_error_handler(status, handler)


# ─── Helpers: values, paging, objects ─────────────────────────────────────────

def iso(value):
    """Stored local time ("YYYY-MM-DD HH:MM[:SS]"), epoch or datetime -> ISO 8601 with offset."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    if isinstance(value, datetime):
        return value.astimezone().isoformat(timespec="seconds")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).astimezone().isoformat(timespec="seconds")
        except ValueError:
            continue
    return value


def eta_seconds(text):
    m = re.fullmatch(r"(\d+)h(\d+)m(\d+)s", text or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def num(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def int_arg(name, default, lo=None, hi=None):
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    if not re.fullmatch(r"\d{1,12}", raw):
        raise APIError(400, "invalid_request", f"{name} must be a whole number.", field=name)
    value = int(raw)
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise APIError(400, "invalid_request", f"{name} must be between {lo} and {hi}.", field=name)
    return value


def _serializer():
    return URLSafeTimedSerializer(ui.app.secret_key, salt="api-cursor")


def _query_fingerprint():
    args = sorted((k, v) for k, v in request.args.items(multi=True) if k not in ("cursor", "limit"))
    return hashlib.sha256(json.dumps([request.path, args]).encode()).hexdigest()[:16]


def offset_from_cursor():
    raw = request.args.get("cursor")
    if not raw:
        return 0
    try:
        data = _serializer().loads(raw, max_age=CURSOR_TTL)
    except BadSignature:
        raise APIError(400, "invalid_cursor", "The cursor is invalid or expired; start the list again.")
    if data.get("q") != _query_fingerprint():
        raise APIError(400, "invalid_cursor", "The cursor belongs to another query.")
    return int(data.get("o", 0))


def next_cursor(offset, count, total):
    return _serializer().dumps({"o": offset + count, "q": _query_fingerprint()}) if offset + count < total else None


def page(items, extra=None):
    """A list answer of already filtered and sorted items."""
    limit = int_arg("limit", LIMIT_DEFAULT, 1, LIMIT_MAX)
    offset = offset_from_cursor()
    chunk = items[offset:offset + limit]
    return dict({"items": chunk, "total": len(items), "next": next_cursor(offset, len(chunk), len(items))}, **(extra or {}))


def sorted_by(items, fields, default=None):
    """Sort by ?sort=&dir= with a {name: key function} map; missing values last."""
    name = request.args.get("sort") or default
    if not name:
        return items
    if name not in fields:
        raise APIError(400, "invalid_request", f"sort must be one of {', '.join(fields)}.", field="sort")
    desc = request.args.get("dir", "asc") == "desc"
    have = [i for i in items if fields[name](i) is not None]
    missing = [i for i in items if fields[name](i) is None]
    return sorted(have, key=fields[name], reverse=desc) + missing


def words_match(key):
    q = request.args.get("q", "")
    return not q.strip() or ui.matches(key, q)


def share_ref(key_or_prefix):
    prefix = key_or_prefix.split("/", 1)[0]
    m = re.fullmatch(r"share(\d+)", prefix)
    return {"number": int(m.group(1)) if m else None, "name": enc.SHARE_NAMES.get(prefix, prefix)}


_PRESET_NAMES = {}


def preset_ref(pid):
    if not pid:
        return None
    if pid not in _PRESET_NAMES:
        _PRESET_NAMES[pid] = presets.name_of(pid)
    return {"id": pid, "name": _PRESET_NAMES[pid]}


def file_rows(keys):
    keys = list(dict.fromkeys(keys))
    rows = {}
    cols = ", ".join(enc.FILE_COLUMNS)
    with closing(ui.db()) as con:
        for i in range(0, len(keys), 900):
            part = keys[i:i + 900]
            for r in con.execute(f"SELECT {cols} FROM files WHERE key IN ({', '.join('?' * len(part))})", part):
                rows[r[0]] = dict(zip(enc.FILE_COLUMNS, r))
    return rows


def file_obj(key, row=None, results=None):
    """The File object of the specification (row: the files table's row, None = not in the inventory)."""
    folder, _, name = key.partition("/")[2].rpartition("/")
    row = row or {}
    codec = row.get("video_codec")
    last = (results if results is not None else enc.load_results()).get(key)
    status = row.get("status")
    return {
        "key": key, "share": share_ref(key), "folder": folder, "name": name,
        "size": row.get("size"), "added": iso(row.get("added")),
        "video": {"codec": codec, "codec_name": ui.codec_name(codec) if codec else None,
                  "optimized": codec in enc.OPTIMIZED_CODECS if codec else (status == "h265" if status else None),
                  "width": row.get("width"), "height": row.get("height"),
                  "resolution": ui.res_label(row.get("width"), row.get("height")),
                  "duration": row.get("duration"), "bitrate": row.get("bitrate")},
        "probe": {"probed": iso(row.get("probed_at")), "error": row.get("probe_error")},
        "status": status, "status_label": enc.STATUS_LABELS.get(status) if status else None,
        "last_result": {"status": last[0], "at": iso(last[1])} if last else None,
    }


def files_for(keys):
    rows, results = file_rows(keys), enc.load_results()
    return {k: file_obj(k, rows.get(k), results) for k in keys}


def path_arg(name="path", required=False):
    """A share or folder inside a share ("share2", "share2/Shows A"), checked; "" when allowed."""
    value = (request.args.get(name) or "").strip().strip("/")
    if not value:
        if required:
            raise APIError(400, "invalid_request", f"{name} is required.", field=name)
        return ""
    prefix = value.split("/", 1)[0]
    parts = value.split("/")
    if prefix not in enc.ROOT_CANDIDATES or any(p in ("", ".", "..") for p in parts) or "\\" in value:
        raise APIError(404, "not_found", "No such share or folder.")
    return value


def filters_from_args():
    """The web UI's file filters (ui.file_filters) from the API's parameters."""
    args = MultiDict(request.args)
    if args.get("added_from") or args.get("added_to"):
        args["added"], args["from"], args["to"] = "custom", args.get("added_from", ""), args.get("added_to", "")
    filters = ui.file_filters(args)
    problem = ui.added_problem(args)
    if problem:
        raise APIError(422, "validation_failed", problem, fields={"added": problem})
    return filters


RESOLUTIONS = {"4K": (3200, 1800, None, None), "1440p": (2200, 1300, 3200, 1800), "1080p": (1700, 900, 2200, 1300),
               "720p": (1100, 650, 1700, 900), "576p": (900, 540, 1100, 650), "480p": (600, 400, 900, 540),
               "SD": (0, 0, 600, 400)}


def extra_sql():
    """Conditions the Files page doesn't have: sizes, bitrates, resolution classes."""
    where, params = [], []
    for name, col, op in (("min_size", "size", ">="), ("max_size", "size", "<="),
                          ("min_bitrate", "bitrate", ">="), ("max_bitrate", "bitrate", "<=")):
        if request.args.get(name):
            where.append(f"{col} {op} ?")
            params.append(int_arg(name, 0))
    res = request.args.getlist("resolution")
    if res:
        parts = []
        for r in res:
            if r not in RESOLUTIONS:
                raise APIError(400, "invalid_request", f"resolution must be one of {', '.join(RESOLUTIONS)}.", field="resolution")
            w, h, w_up, h_up = RESOLUTIONS[r]
            cond = "(width >= ? OR height >= ?)"
            p = [w, h]
            if w_up is not None:
                cond += " AND NOT (width >= ? OR height >= ?)"
                p += [w_up, h_up]
            parts.append(f"(width IS NOT NULL AND {cond})")
            params += p
        where.append("(" + " OR ".join(parts) + ")")
    return where, params


def worker_state():
    cur = enc.current_progress()
    if cur:
        return "encoding"
    if enc.is_paused() and enc.queue_items():
        return "paused"
    wait = enc.worker_wait()
    if wait:
        return "waiting"
    if Path("/.dockerenv").exists() and not ui.encodes_here() and not enc.WORKER_LOCK.exists():
        return "idle"
    return "idle"


def current_obj(cur=None):
    cur = cur if cur is not None else enc.current_progress()
    if not cur:
        return None
    items = enc.queue_items()
    preset = next((i["preset"] for i in items if i["key"] == cur["key"] and i["state"] == "encoding"), "")
    try:
        started = iso(datetime.fromtimestamp(int(enc.CURRENT_FILE.read_text().split("\t")[3])))
    except (OSError, IndexError, ValueError):
        started = None
    return {"file": files_for([cur["key"]])[cur["key"]], "position": int(cur["i"]) if cur["i"].isdigit() else None,
            "of": int(cur["n"]) if cur["n"].isdigit() else None, "percent": num(cur.get("pct")),
            "fps": num(cur.get("fps")), "eta_seconds": eta_seconds(cur.get("eta")),
            "started": started, "preset": preset_ref(preset)}


def saved_bytes():
    return sum(o - n for _, _, _, sizes in enc.replace_records().values() if sizes
               for o, n in [sizes] if o and n and o > n)


# ─── Phase 1 routes ───────────────────────────────────────────────────────────
FILE_EXAMPLE = {"key": "share2/Northwind Station (2018)/Season 02/Northwind.Station.S02E01.mkv",
                "share": {"number": 2, "name": "Shows"}, "folder": "Northwind Station (2018)/Season 02",
                "name": "Northwind.Station.S02E01.mkv", "size": 2147483648, "added": "2026-10-30T21:14:00+01:00",
                "video": {"codec": "h264", "codec_name": "H.264 (AVC)", "optimized": False, "width": 1920,
                          "height": 1080, "resolution": "1080p", "duration": 2712.4, "bitrate": 6334000},
                "probe": {"probed": "2026-10-31T02:10:00+01:00", "error": None},
                "status": "offer", "status_label": "Offered", "last_result": None}
PAGING = [("limit", "integer", f"Items per page (1–{LIMIT_MAX}, default {LIMIT_DEFAULT})"),
          ("cursor", "string", "The next page, from the previous answer's next")]


@route("GET", "/", None, "API and token information",
       response={"api_version": "1.0", "handlebreaks_version": "4.2.0",
                 "token": {"id": "k3m9q2xa", "name": "Homepage", "scopes": ["status:read"], "expires": None},
                 "docs": "/api/v1/openapi.json"})
def info():
    t = g.token
    return {"api_version": API_VERSION, "handlebreaks_version": enc.__version__,
            "token": {"id": t["id"], "name": t["name"], "scopes": t["scopes"], "expires": t.get("expires")},
            "docs": request.script_root + bp.url_prefix + "/openapi.json"}


@route("GET", "/summary", "status:read",
       "Flat numbers for dashboards; fields of scopes the token lacks are null",
       response={"encoding": {"active": True, "file": "Northwind.Station.S02E03.mkv", "percent": 41.2, "fps": 38.5,
                              "eta_seconds": 1260, "position": 3, "of": 12},
                 "worker": {"state": "encoding", "paused": False}, "queue": {"waiting": 9, "next_run": 4},
                 "review": {"waiting": 5, "saved_bytes": 9126805504}, "history": {"done_24h": 7, "failed_24h": 1},
                 "library": {"files": 70312, "optimized_percent": 38.4, "to_encode": 41022},
                 "saved": {"bytes": 1849267937280}, "offline": [], "scanning": False})
def summary():
    scopes = set(g.token["scopes"])
    cur = enc.current_progress()
    out = {"encoding": {"active": bool(cur), "file": cur["key"].rsplit("/", 1)[-1] if cur else None,
                        "percent": num(cur.get("pct")) if cur else None, "fps": num(cur.get("fps")) if cur else None,
                        "eta_seconds": eta_seconds(cur.get("eta")) if cur else None,
                        "position": int(cur["i"]) if cur and cur["i"].isdigit() else None,
                        "of": int(cur["n"]) if cur and cur["n"].isdigit() else None},
           "worker": {"state": worker_state(), "paused": enc.is_paused()},
           "queue": None, "review": None, "history": None, "library": None, "saved": None,
           "offline": ui.offline_now(), "scanning": bool(ui.SCAN["running"] or enc.scan_progress())}
    if "queue:read" in scopes:
        items = [i for i in enc.queue_items() if i["state"] == "waiting"]
        out["queue"] = {"waiting": len(items), "next_run": sum(not i["run"] for i in items)}
    if "review:read" in scopes:
        review = ui.awaiting_review()
        out["review"] = {"waiting": len(review),
                         "saved_bytes": sum(max(0, (r["original_size"] or 0) - r["size"]) for r in review)}
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
        with closing(ui.db()) as con:
            counts = dict(con.execute("SELECT status, COUNT(*) FROM results WHERE at >= ? GROUP BY status", (since,)))
        out["history"] = {"done_24h": counts.get("OK", 0), "failed_24h": counts.get("FAIL", 0)}
    if "stats:read" in scopes:
        total, _, _, _, statuses, _ = ui.library_summary()
        out["library"] = {"files": total, "optimized_percent": round(statuses.get("h265", 0) / total * 100, 1) if total else 0,
                          "to_encode": statuses.get("offer", 0)}
        out["saved"] = {"bytes": saved_bytes()}
    return out


@route("GET", "/status", "status:read", "Current encode, worker, offline shares, scans and replaces",
       response={"current": None, "worker": {"state": "idle", "paused": False, "stoppable": False, "waiting_for": None},
                 "offline": [], "scan": {"running": True, "label": "Scan of all shares", "started": "2026-11-06T09:00+01:00",
                                         "phase": "Reading files", "percent": 42.0, "detail": "1,204 of 2,866 files read",
                                         "time_left": "about 6 min", "last_finished": None},
                 "replaces": {"waiting": 0, "moving": 0, "checking": 0}})
def status():
    wait = enc.worker_wait()
    offline = []
    for name in ui.offline_now():
        prefix = next((p for p, n in enc.SHARE_NAMES.items() if n == name), None)
        offline.append({"share": share_ref(prefix) if prefix else None, "name": name})
    rp = ui.replace_progress()
    return {"current": current_obj(),
            "worker": {"state": worker_state(), "paused": enc.is_paused(), "stoppable": enc.worker_can_stop(),
                       "waiting_for": {"kind": wait["kind"], "share": share_ref(wait["prefix"]) if wait.get("prefix") else None,
                                       "error": wait.get("error") or None} if wait else None},
            "offline": offline,
            "scan": scan_obj(),
            "replaces": {k: rp.get(k, 0) for k in ("waiting", "moving", "checking")}}


def scan_obj():
    """The running scan (web UI or command line) with its phase and progress (webui.scan_view)."""
    v = ui.scan_view()
    return {"running": bool(v), "label": v["label"] if v else None, "started": v["started"] or None if v else None,
            "phase": v["phase"] or None if v else None, "percent": v["pct"] if v else None,
            "detail": v["detail"] or None if v else None, "time_left": v["eta"] or None if v else None,
            "last_finished": iso(enc.last_scan())}


@route("GET", "/queue", "queue:read", "The queue in the worker's order",
       params=[("state", "string, repeatable", "waiting, encoding, removed (default: all three)"),
               ("q", "string", "Words that must all be part of the file's key"),
               ("sort", "string", "order (default), file, codec, resolution, bitrate, preset, state"),
               ("dir", "string", "asc or desc")] + PAGING,
       response={"items": [{"position": 4, "state": "waiting", "run": True, "file": FILE_EXAMPLE, "preset": None}],
                 "total": 1, "next": None})
def queue():
    states = set(request.args.getlist("state")) or {"waiting", "encoding", "removed"}
    if states - {"waiting", "encoding", "removed"}:
        raise APIError(400, "invalid_request", "state must be waiting, encoding or removed.", field="state")
    items = [(n, i) for n, i in enumerate(enc.queue_items(), 1) if i["state"] in states and words_match(i["key"])]
    files = files_for([i["key"] for _, i in items])
    rows = [{"position": n, "state": i["state"], "run": i["run"], "file": files[i["key"]], "preset": preset_ref(i["preset"])}
            for n, i in items]
    rows = sorted_by(rows, {"order": lambda r: r["position"], "file": lambda r: r["file"]["name"].lower(),
                            "codec": lambda r: r["file"]["video"]["codec"], "resolution": lambda r: r["file"]["video"]["width"],
                            "bitrate": lambda r: r["file"]["video"]["bitrate"],
                            "preset": lambda r: (r["preset"] or {}).get("name"), "state": lambda r: r["state"]})
    return page(rows)


@route("GET", "/shares", "library:read", "The shares (without their paths)",
       response={"items": [{"number": 2, "name": "Shows", "reachable": True, "preset": None, "files": 31877,
                            "last_scan": "2026-11-05T03:00:00+01:00", "optimized_percent": 41.0}], "total": 1, "next": None})
def shares():
    info = ui.share_info()
    offline = set(ui.offline_now())
    with closing(ui.db()) as con:
        opt = dict(con.execute("SELECT substr(key, 1, instr(key, '/') - 1), SUM(status = 'h265') FROM files GROUP BY 1"))
    cfg = enc.parse_config(enc.CONFIG_FILE) if enc.CONFIG_FILE.exists() else {}
    out = []
    for sh in enc.shares(cfg):
        p = sh["prefix"]
        n = info.get(p, {}).get("files", 0)
        out.append({"number": sh["n"], "name": sh["name"], "reachable": sh["name"] not in offline,
                    "preset": preset_ref(sh.get("preset")), "files": n, "last_scan": iso(info.get(p, {}).get("scanned")),
                    "optimized_percent": round((opt.get(p) or 0) / n * 100, 1) if n else 0})
    return page(out)


FILTER_PARAMS = [("q", "string", "Words that must all be part of the key"),
                 ("codec", "string, repeatable", "Codec names, unknown, group:good, group:other"),
                 ("status", "string, repeatable", "offer, queued, waiting, h265, bad, skipped, baseline"),
                 ("added", "string", "A range such as 24h, 7d, 3m-1m, -1y"),
                 ("added_from", "date", "YYYY-MM-DD"), ("added_to", "date", "YYYY-MM-DD"),
                 ("min_size", "integer", "Bytes"), ("max_size", "integer", "Bytes"),
                 ("min_bitrate", "integer", "bit/s"), ("max_bitrate", "integer", "bit/s"),
                 ("resolution", "string, repeatable", "4K, 1440p, 1080p, 720p, 576p, 480p, SD")]


@route("GET", "/folders", "library:read", "One level of the folder tree with counts, and its files",
       params=[("path", "string", "share<N> or share<N>/<folder>; empty = the shares")] + FILTER_PARAMS[:6] + PAGING[1:],
       response={"path": "share2/Northwind Station (2018)",
                 "folders": [{"path": "share2/Northwind Station (2018)/Season 02", "name": "Season 02", "files": 10,
                              "size": 21474836480, "optimized": 2, "unknown": 0, "other": 8,
                              "states": {"offered": 6, "queued": 1, "to_review": 1, "failed": 0}}],
                 "files": {"items": [], "total": 0, "next": None}})
def folders():
    path = path_arg()
    filters = filters_from_args()
    offset = offset_from_cursor()
    dirs, files, total = ui.tree_level(path, filters, enc.pending_keys(), offset=offset)
    results = enc.load_results()
    names = {"offered": "offered", "queued": "queued", "to review": "to_review", "failed": "failed"}
    out = []
    for d in dirs:
        states = {v: 0 for v in names.values()}
        for n, label in d["states"]:
            states[names[label]] = n
        out.append({"path": d["path"], "name": d["name"], "files": d["count"], "size": d["size"],
                    "optimized": d["optimized"], "unknown": d["unknown"], "other": d["other"], "states": states})
    items = [file_obj(f["key"], f, results) for f in files]
    return {"path": path, "folders": out,
            "files": {"items": items, "total": total, "next": next_cursor(offset, len(items), total)}}


FILE_SORT = {"name": "key", "codec": "video_codec", "resolution": "width", "bitrate": "bitrate", "duration": "duration",
             "size": "size", "added": "added", "status": "status"}


@route("GET", "/files", "library:read", "Search files across the library",
       params=[("path", "string", "Limit to a share or folder (recursive)")] + FILTER_PARAMS
       + [("sort", "string", ", ".join(FILE_SORT)), ("dir", "string", "asc or desc"),
          ("facets", "1", "Add counts per filter choice")] + PAGING,
       response={"items": [FILE_EXAMPLE], "total": 1, "next": None})
def files():
    path = path_arg()
    filters = filters_from_args()
    clause, params = ui.filter_sql(filters, path)
    more, more_params = extra_sql()
    if more:
        clause = (clause + " AND " if clause else " WHERE ") + " AND ".join(more)
        params = params + more_params
    sort = request.args.get("sort") or "name"
    if sort not in FILE_SORT:
        raise APIError(400, "invalid_request", f"sort must be one of {', '.join(FILE_SORT)}.", field="sort")
    col, desc = FILE_SORT[sort], request.args.get("dir") == "desc"
    limit = int_arg("limit", LIMIT_DEFAULT, 1, LIMIT_MAX)
    offset = offset_from_cursor()
    cols = ", ".join(enc.FILE_COLUMNS)
    with closing(ui.db()) as con:
        total = con.execute(f"SELECT COUNT(*) FROM files{clause}", params).fetchone()[0]
        rows = [dict(zip(enc.FILE_COLUMNS, r)) for r in con.execute(
            f"SELECT {cols} FROM files{clause} ORDER BY {col} IS NULL, {col} {'DESC' if desc else 'ASC'}, key "
            f"LIMIT ? OFFSET ?", params + [limit, offset])]
    results = enc.load_results()
    out = {"items": [file_obj(r["key"], r, results) for r in rows], "total": total,
           "next": next_cursor(offset, len(rows), total)}
    if request.args.get("facets") == "1":
        f = ui.facet_counts(filters)
        out["facets"] = {"codec": dict(f["codecs"], unknown=f["unknown"], **f["groups"]),
                         "status": dict(f["statuses"]), "added": dict(f["added"])}
    return out


@route("GET", "/file", "library:read", "One file with its queue entry, finished encode and history",
       params=[("key", "string", "The file's key (required)")],
       response=dict(FILE_EXAMPLE, queue=None, encode=None, history=[]))
def file():
    key = request.args.get("key", "")
    rows = file_rows([key])
    if key not in rows:
        raise APIError(404, "not_found", "No file with this key.")
    out = file_obj(key, rows[key])
    entry = next(((n, i) for n, i in enumerate(enc.queue_items(), 1) if i["key"] == key and i["state"] in ("waiting", "encoding")), None)
    out["queue"] = {"position": entry[0], "state": entry[1]["state"], "preset": preset_ref(entry[1]["preset"])} if entry else None
    review = next((r for r in ui.awaiting_review() if r["key"] == key), None)
    out["encode"] = review_encode(review) if review else None
    out["history"] = [{"at": iso(h["at"]), "status": h["status"]} for h in enc.history_entries() if h["key"] == key][:20]
    return out


def review_encode(r):
    enc_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    name = enc.encoded_files(enc_dir).get(Path(r["key"]).stem.lower()) if enc_dir else None
    return {"name": name, "size": r["size"], "bitrate": r.get("enc_bitrate"), "finished": iso(r["at"]),
            "preset": preset_ref(r.get("preset"))}


@route("GET", "/review", "review:read", "Finished encodes awaiting review",
       params=[("q", "string", "Words in the key"),
               ("sort", "string", "file, preset, codec, resolution, bitrate, size, saved, finished"),
               ("dir", "string", "asc or desc")] + PAGING,
       response={"items": [{"file": FILE_EXAMPLE, "encode": {"name": "Northwind.Station.S02E01.x265.mkv", "size": 734003200,
                                                             "bitrate": 2165000, "finished": "2026-11-05T14:20:00+01:00",
                                                             "preset": {"id": "1080p-x265", "name": "1080p x265"}},
                            "saved_bytes": 1413480448, "saved_percent": 65.8, "replace": None}],
                 "total": 1, "next": None, "saved_bytes": 1413480448})
def review():
    rows = [r for r in ui.awaiting_review() if words_match(r["key"])]
    files = files_for([r["key"] for r in rows])
    open_jobs = enc.open_replaces()
    enc_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    names = enc.encoded_files(enc_dir) if enc_dir else {}
    out = []
    for r in rows:
        saved = (r["original_size"] - r["size"]) if r["original_size"] else None
        job = open_jobs.get(r["key"])
        out.append({"file": files[r["key"]],
                    "encode": {"name": names.get(Path(r["key"]).stem.lower()), "size": r["size"], "bitrate": r.get("enc_bitrate"),
                               "finished": iso(r["at"]), "preset": preset_ref(r.get("preset"))},
                    "saved_bytes": saved, "saved_percent": round(saved / r["original_size"] * 100, 1) if saved is not None else None,
                    "replace": {"id": job[0], "state": "moving" if job[1] else "waiting"} if job else None})
    out = sorted_by(out, {"file": lambda r: r["file"]["name"].lower(), "preset": lambda r: (r["encode"]["preset"] or {}).get("name"),
                          "codec": lambda r: r["file"]["video"]["codec"], "resolution": lambda r: r["file"]["video"]["width"],
                          "bitrate": lambda r: r["file"]["video"]["bitrate"], "size": lambda r: r["encode"]["size"],
                          "saved": lambda r: r["saved_bytes"], "finished": lambda r: r["encode"]["finished"]})
    return page(out, {"saved_bytes": sum(max(0, r["saved_bytes"] or 0) for r in out)})


def fail_logs():
    """[{"id", "at", "path", "key"}] of logs/fail-*.log, newest first."""
    out = []
    for f in enc.LOG_DIR.glob("fail-*.log") if enc.LOG_DIR.exists() else []:
        m = FAIL_LOG_RE.fullmatch(f.name)
        if not m or not f.is_file() or f.is_symlink():
            continue
        at = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        key = None
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            if first.startswith("# Handlebreaks: "):
                key = first[16:].strip()
        except OSError:
            pass
        out.append({"id": "f-" + m.group(1), "at": at, "path": f, "key": key})
    out.sort(key=lambda x: x["at"], reverse=True)
    if any(x["key"] is None for x in out):            # older logs: the FAIL result within 2 minutes
        with closing(ui.db()) as con:
            fails = [(datetime.strptime(a, "%Y-%m-%d %H:%M"), k) for a, k in
                     con.execute("SELECT at, key FROM results WHERE status = 'FAIL'") if len(a) == 16]
        for x in out:
            if x["key"] is None:
                near = [(abs((a - x["at"]).total_seconds()), k) for a, k in fails if abs((a - x["at"]).total_seconds()) <= 120]
                x["key"] = min(near)[1] if near else None
    return out


@route("GET", "/history", "review:read", "Encode results and finished replaces, newest first",
       params=[("status", "string", "OK, FAIL, STOPPED, MISSING, EXISTS, REPLACED"), ("q", "string", "Words in the key"),
               ("since", "date or time", "Only entries from then on (also older than the history limit)"),
               ("include_dismissed", "1", "Also entries dismissed on the Status page"),
               ("sort", "string", "at (default), file, status"), ("dir", "string", "asc or desc")] + PAGING,
       response={"items": [{"at": "2026-11-05T14:02:00+01:00", "status": "FAIL", "file": FILE_EXAMPLE, "can_retry": True,
                            "log": "f-20261105-140211", "replaced_by": None, "plex": None}], "total": 1, "next": None})
def history():
    status = request.args.get("status", "")
    if status and status not in ("OK", "FAIL", "STOPPED", "MISSING", "EXISTS", "REPLACED"):
        raise APIError(400, "invalid_request", "Unknown status.", field="status")
    since = request.args.get("since", "")
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise APIError(400, "invalid_request", "since must be a date or ISO 8601 time.", field="since")
        since = (since_dt.astimezone().replace(tzinfo=None) if since_dt.tzinfo else since_dt).strftime("%Y-%m-%d %H:%M")
        entries = [h for h in enc.history_entries(result=status, older_than=0) if h["at"] >= since]
        entries += [h for h in enc.history_entries(result=status) if h["at"] >= since]
        seen, uniq = set(), []
        for h in sorted(entries, key=lambda h: h["at"], reverse=True):
            if (h["at"], h["status"], h["key"]) not in seen:
                seen.add((h["at"], h["status"], h["key"]))
                uniq.append(h)
        entries = uniq
    else:
        entries = enc.history_entries(result=status)
    if not request.args.get("include_dismissed") == "1":
        dismissed = enc.dismissed_map()
        entries = [h for h in entries if dismissed.get(h["key"], "") < h["at"]]
    entries = [h for h in entries if words_match(h["key"])]
    files = files_for([h["key"] for h in entries] + [h["new_key"] for h in entries if h.get("new_key")])
    logs = {}
    if any(h["status"] == "FAIL" for h in entries):
        for x in fail_logs():
            if x["key"]:
                logs.setdefault(x["key"], []).append(x)
    out = []
    for h in entries:
        log = None
        if h["status"] == "FAIL":
            at = datetime.strptime(h["at"], "%Y-%m-%d %H:%M")
            near = [x for x in logs.get(h["key"], []) if abs((x["at"] - at).total_seconds()) <= 120]
            log = near[0]["id"] if near else None
        out.append({"at": iso(h["at"]), "status": h["status"], "file": files[h["key"]], "can_retry": h["can_retry"],
                    "log": log, "replaced_by": files.get(h["new_key"]) if h.get("new_key") else None,
                    "plex": h.get("plex") if h["status"] == "REPLACED" else None})
    out = sorted_by(out, {"at": lambda r: r["at"], "file": lambda r: r["file"]["name"].lower(), "status": lambda r: r["status"]},
                    default=None)
    return page(out)


@route("GET", "/replace/{job_id}", "review:read", "The state of one replace job",
       response={"id": "20261105-142000-123456", "state": "done", "message": "Replaced.", "plex": "ok", "finished": None})
def replace_job(job_id):
    if not re.fullmatch(r"[\d-]{6,40}", job_id):
        raise APIError(404, "not_found", "Unknown job.")
    job = enc.replace_status(job_id)
    if job.get("state") == "failed" and job.get("message") == "Unknown job.":
        raise APIError(404, "not_found", "Unknown job.")
    state = job.get("state")
    if state == "pending":
        state = "moving" if job.get("started") else "waiting"
    elif state == "done" and not job.get("synced"):
        state = "checking"
    plex = job.get("plex") or {}
    return {"id": job_id, "state": state, "message": job.get("message"), "key": job.get("key"),
            "new_key": job.get("new_key"), "plex": plex.get("state") if isinstance(plex, dict) else None,
            "finished": iso(job.get("finished"))}


def preset_obj(pid, p, usage, default, caps, remote):
    compat = presets.compatibility(p, caps, remote, presets.file_format(pid))
    audio = []
    for a in p.get("AudioList") or []:
        audio.append(f"{a.get('AudioEncoder', '?')} {a.get('AudioMixdown', '')}".strip())
    return {"id": pid, "name": p.get("PresetName", pid), "default": pid == default,
            "used_by_shares": usage.get(pid, []),
            "summary": {"encoder": p.get("VideoEncoder"), "speed": p.get("VideoPreset") or None,
                        "quality": presets.quality_tag(p), "container": (p.get("FileFormat") or "").replace("av_", "") or None,
                        "resolution_limit": f"{p.get('PictureWidth')}x{p.get('PictureHeight')}"
                        if p.get("PictureWidth") and p.get("PictureHeight") else None,
                        "audio": audio, "text": presets.summary(p)},
            "compatible": not compat.get("notes"), "notes": compat.get("notes", [])}


@route("GET", "/presets", "presets:read", "The presets with a summary of their settings",
       response={"items": [{"id": "1080p-x265", "name": "1080p x265 (Ger/Eng) RF20", "default": True, "used_by_shares": ["Shows"],
                            "summary": {"encoder": "x265_10bit", "speed": "slow", "quality": "rf22", "container": "mkv",
                                        "resolution_limit": "1920x1080", "audio": ["av_aac stereo"], "text": "…"},
                            "compatible": True, "notes": []}], "total": 1, "next": None})
def preset_list():
    usage, default = ui.preset_usage()
    caps, remote = presets.editor_capabilities(ui.encodes_here())
    return page([preset_obj(pid, p, usage, default, caps, remote) for pid, p in presets.all_presets()])


@route("GET", "/presets/{preset_id}", "presets:read", "One preset with its complete HandBrake settings",
       response={"id": "1080p-x265", "name": "1080p x265 (Ger/Eng) RF20", "handbrake": {"PresetName": "…"}})
def preset_one(preset_id):
    if not re.fullmatch(r"[\w.-]{1,80}", preset_id) or not presets.exists(preset_id):
        raise APIError(404, "not_found", "No such preset.")
    usage, default = ui.preset_usage()
    caps, remote = presets.editor_capabilities(ui.encodes_here())
    p = presets.load(preset_id)
    return dict(preset_obj(preset_id, p, usage, default, caps, remote), handbrake=p)


@route("GET", "/stats", "stats:read", "Statistics of the library and of saved space",
       response={"library": {"files": 70312, "size": 211106232532992, "optimized_files": 27020,
                             "optimized_size": 81064793292800, "codecs": {"h264": 39871}, "statuses": {"offer": 41022},
                             "resolutions": {"1080p": 41552}},
                 "encodes": {"total": 2140, "failed": 37, "per_day": [{"date": "2026-11-04", "ok": 14, "failed": 1}]},
                 "saved": {"bytes": 1849267937280, "replaced_files": 1980}, "kept_originals": {"files": 12, "size": 24696061952}})
def stats():
    total, total_size, opt_size, codecs, statuses, _ = ui.library_summary()
    with closing(ui.db()) as con:
        res_rows = con.execute("SELECT width, height, COUNT(*) FROM files WHERE width IS NOT NULL GROUP BY width, height").fetchall()
        results = dict(con.execute("SELECT status, COUNT(*) FROM results GROUP BY status").fetchall())
        start = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        per_day = con.execute("SELECT substr(at, 1, 10), SUM(status = 'OK'), SUM(status = 'FAIL') FROM results "
                              "WHERE at >= ? GROUP BY 1 ORDER BY 1", (start,)).fetchall()
    resolutions = {}
    for w, h, n in res_rows:
        label = ui.res_label(w, h)
        resolutions[label] = resolutions.get(label, 0) + n
    records = enc.replace_records()
    kept = ui.kept_list()
    return {"library": {"files": total, "size": total_size, "optimized_files": statuses.get("h265", 0), "optimized_size": opt_size,
                        "codecs": {c: n for c, n in codecs}, "statuses": statuses, "resolutions": resolutions},
            "encodes": {"total": sum(results.values()), "failed": results.get("FAIL", 0),
                        "per_day": [{"date": d, "ok": ok or 0, "failed": f or 0} for d, ok, f in per_day]},
            "saved": {"bytes": saved_bytes(), "replaced_files": len(records)},
            "kept_originals": {"files": len(kept), "size": sum(k.get("size") or 0 for k in kept)}}


@route("GET", "/events", "events:read", "The notification feed of the last 7 days",
       params=[("after", "integer", "Only events after this id; without it the answer only holds last_id")],
       response={"items": [{"id": 9182, "at": "2026-11-05T14:02:11+01:00", "kind": "encode_fail", "group": "encodes",
                            "level": "error", "title": "Encode failed", "file": {"key": "…", "name": "…"}, "detail": "…"}],
                 "last_id": 9182})
def events():
    after = int_arg("after", None, 0)
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    last, rows = enc.events_after(after, since)
    items = []
    for ev in rows if after is not None else []:
        group, level, title, _ = ui.EVENT_KINDS.get(ev["kind"], ("", "info", ev["kind"], ""))
        items.append({"id": ev["id"], "at": iso(ev["at"]), "kind": ev["kind"], "group": group or None,
                      "level": ev.get("level") or level, "title": title,
                      "file": {"key": ev["key"], "name": ev["key"].rsplit("/", 1)[-1]} if ev.get("key") else None,
                      "detail": ev.get("detail") or None})
    return {"items": items, "last_id": last}


@route("GET", "/logs/failures", "logs:read", "HandBrake's logs of failed encodes, newest first",
       response={"items": [{"id": "f-20261105-140211", "at": "2026-11-05T14:02:11+01:00",
                            "file": {"key": "…", "name": "…"}, "size": 48213}], "total": 1, "next": None})
def failure_logs():
    out = []
    for x in fail_logs():
        try:
            size = x["path"].stat().st_size
        except OSError:
            continue
        out.append({"id": x["id"], "at": iso(x["at"]),
                    "file": {"key": x["key"], "name": x["key"].rsplit("/", 1)[-1]} if x["key"] else None, "size": size})
    return page(out)


@route("GET", "/logs/failures/{log_id}", "logs:read", "One log as text/plain (the last 2 MB)", response="(text)")
def failure_log(log_id):
    m = re.fullmatch(r"f-(\d{8}-\d{6})", log_id)
    f = enc.LOG_DIR / f"fail-{m.group(1)}.log" if m else None
    if not f or not f.is_file() or f.is_symlink():
        raise APIError(404, "not_found", "No such log.")
    with open(f, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - 2 * 1024 * 1024))
        data = fh.read()
    return Response(data.decode("utf-8", "replace"), mimetype="text/plain")


@route("GET", "/system", "system:read", "Versions, HandBrakeCLI, free space in the output folder (no paths or settings)",
       response={"handlebreaks_version": "4.2.0", "api_version": "1.0", "mode": "docker-ui-native-worker",
                 "handbrake": {"here": "1.11.2", "worker": "1.11.2", "worker_platform": "macOS"}, "ffprobe": True,
                 "python": "3.13.5", "output_folder": {"reachable": True, "free_bytes": 1294018101248, "total_bytes": 7999995002880},
                 "shares": [{"number": 2, "name": "Shows", "reachable": True}], "uptime_seconds": 61234})
def system():
    here = presets.capabilities()
    worker = None if ui.encodes_here() else presets.worker_capabilities()
    docker = Path("/.dockerenv").exists()
    mode = "docker" if docker and ui.encodes_here() else "docker-ui-native-worker" if docker else "native"
    out_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    disk = None
    if out_dir:
        try:
            du = shutil.disk_usage(out_dir)
            disk = {"reachable": True, "free_bytes": du.free, "total_bytes": du.total}
        except OSError:
            pass
    offline = set(ui.offline_now())
    cfg = enc.parse_config(enc.CONFIG_FILE) if enc.CONFIG_FILE.exists() else {}
    hb = lambda c: c.get("handbrake") if c and c.get("available") else None
    return {"handlebreaks_version": enc.__version__, "api_version": API_VERSION, "mode": mode,
            "handbrake": {"here": hb(here), "worker": hb(worker) if worker else hb(here),
                          "worker_platform": (worker or here or {}).get("platform")},
            "ffprobe": bool(getattr(enc, "FFPROBE", None)), "python": sys.version.split()[0],
            "output_folder": disk or {"reachable": False, "free_bytes": None, "total_bytes": None},
            "shares": [{"number": sh["n"], "name": sh["name"], "reachable": sh["name"] not in offline} for sh in enc.shares(cfg)],
            "uptime_seconds": int(time.time() - ui.STARTED)}


# ─── OpenAPI ──────────────────────────────────────────────────────────────────

def _schema_of(example):
    if isinstance(example, bool):
        return {"type": "boolean"}
    if isinstance(example, int):
        return {"type": "integer"}
    if isinstance(example, float):
        return {"type": "number"}
    if isinstance(example, str):
        return {"type": "string"}
    if example is None:
        return {"nullable": True}
    if isinstance(example, list):
        return {"type": "array", "items": _schema_of(example[0]) if example else {}}
    return {"type": "object", "properties": {k: _schema_of(v) for k, v in example.items()}}


def openapi_doc(prefix):
    paths = {}
    for r in ROUTES:
        params = [{"name": n, "in": "query", "description": f"{d} ({t})", "schema": {"type": "string"}} for n, t, d in r["params"]]
        params += [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}} for n in re.findall(r"\{(\w+)\}", r["path"])]
        text = isinstance(r["response"], str)
        op = {"summary": r["summary"], "operationId": r["endpoint"].split(".", 1)[1], "parameters": params,
              "security": [{"bearer": [r["scope"]] if r["scope"] else []}],
              "x-scope": r["scope"],
              "responses": {"200": {"description": "OK", "content": {"text/plain" if text else "application/json":
                                    {"schema": {"type": "string"} if text else _schema_of(r["response"]),
                                     **({} if text else {"example": r["response"]})}}},
                            "401": {"$ref": "#/components/responses/Error"}, "403": {"$ref": "#/components/responses/Error"},
                            "429": {"$ref": "#/components/responses/Error"}}}
        paths.setdefault(r["path"], {})[r["method"].lower()] = op
    err = {"type": "object", "properties": {"error": {"type": "object", "properties": {
        "code": {"type": "string"}, "message": {"type": "string"}, "details": {"type": "object"}}}}}
    return {"openapi": "3.1.0",
            "info": {"title": "Handlebreaks API", "version": API_VERSION,
                     "description": "Scoped API of Handlebreaks. Every request needs Authorization: Bearer hb_<id>_<secret>."},
            "servers": [{"url": prefix}], "paths": paths,
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
                           "responses": {"Error": {"description": "Error", "content": {"application/json": {"schema": err}}}},
                           "x-scopes": {s: d for s, (_, d) in apitokens.SCOPES.items()}}}


@route("GET", "/openapi.json", None, "This API's OpenAPI 3.1 description", response={"openapi": "3.1.0"})
def openapi():
    return openapi_doc(request.script_root + bp.url_prefix)


# ─── Writing: request bodies, safe retries, audit ─────────────────────────────
MAX_BODY = 1024 * 1024
MAX_KEYS = 5000
IDEMPOTENCY_TTL = 24 * 3600
_IDEMPOTENT = {}                                # (token id, key) -> (body hash, status, body, time)


def body():
    """The JSON object of a write request (415 without JSON, 413 when too large)."""
    if request.mimetype != "application/json":
        raise APIError(415, "unsupported_media_type", "Send the request as JSON (Content-Type: application/json).")
    if (request.content_length or 0) > MAX_BODY:
        raise APIError(413, "too_large", "The request body is larger than 1 MB.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError(400, "invalid_json", "The body must be a JSON object.")
    return data


def str_list(data, name, required=False):
    value = data.get(name, [])
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise APIError(422, "validation_failed", f"{name} must be a list of strings.", fields={name: "list of strings"})
    if len(value) > MAX_KEYS:
        raise APIError(413, "too_large", f"At most {MAX_KEYS} entries per request.")
    if required and not value:
        raise APIError(422, "validation_failed", f"{name} is empty.", fields={name: "required"})
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def preset_arg(data):
    pid = data.get("preset")
    if pid in (None, ""):
        return None
    if not isinstance(pid, str) or not presets.exists(pid):
        raise APIError(422, "validation_failed", "Unknown preset.", fields={"preset": "unknown"})
    return pid


def outcome(done, skipped=(), rejected=(), **extra):
    """The answer of a bulk operation; 422 when everything was rejected."""
    out = dict({"done": list(done), "skipped": [{"key": k, "reason": r} for k, r in skipped],
                "rejected": [{"key": k, "reason": r} for k, r in rejected]}, **extra)
    if rejected and not done and not skipped:
        raise APIError(422, "validation_failed", "Nothing could be done.", items=out["rejected"])
    return out


def audit(action, count=None):
    """Record a write: activity page (action) and a notification naming the token."""
    g.action = action if count is None else f"{action} ({count:,})"
    try:
        ui.notify("api_write", None, f"API ({g.token['name']}): {g.action}")
    except Exception as e:
        print(f"API notification failed: {e}", file=sys.stderr)


def idempotent(fn):
    """Idempotency-Key: the first answer for a key is kept 24 hours per token and given again for
    a retry with the same body (header Idempotent-Replayed); another body with it is 422."""
    def wrapper(*args, **kwargs):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return fn(*args, **kwargs)
        if len(key) > 100:
            raise APIError(400, "invalid_request", "Idempotency-Key can have at most 100 characters.")
        digest = hashlib.sha256(request.get_data()).hexdigest()
        slot = (g.token["id"], key)
        now = time.time()
        with _LOCK:
            for k in [k for k, v in _IDEMPOTENT.items() if now - v[3] > IDEMPOTENCY_TTL]:
                del _IDEMPOTENT[k]
            hit = _IDEMPOTENT.get(slot)
        if hit:
            if hit[0] != digest:
                raise APIError(422, "idempotency_key_reused", "This Idempotency-Key was used with another request body.")
            resp = Response(hit[2], status=hit[1], mimetype="application/json")
            resp.headers["Idempotent-Replayed"] = "true"
            return resp
        result = fn(*args, **kwargs)
        resp = result if isinstance(result, Response) else ui.app.make_response(result)
        if resp.status_code < 500:
            with _LOCK:
                _IDEMPOTENT[slot] = (digest, resp.status_code, resp.get_data(), now)
        return resp
    wrapper.__name__ = fn.__name__
    return wrapper


BULK = {"done": ["share2/…S02E01.mkv"], "skipped": [{"key": "share2/…S02E02.mkv", "reason": "already_queued"}],
        "rejected": [{"key": "share9/x.mkv", "reason": "unknown_key"}]}


# ─── Phase 2: queue, encoding, scans, review ──────────────────────────────────

@route("POST", "/queue", "queue:write", "Queue files and folders (folders expanded with the filters, offered files only)",
       params=[("keys", "body: list", "File keys"), ("folders", "body: list", "share<N>/<folder> (recursive)"),
               ("filters", "body: object", "The filters of GET /files (codec, status, added, q …)"),
               ("preset", "body: string", "Preset id; null = the share's preset")],
       response=BULK)
@idempotent
def queue_add():
    data = body()
    keys, folders = str_list(data, "keys"), str_list(data, "folders")
    if not keys and not folders:
        raise APIError(422, "validation_failed", "Name keys or folders.", fields={"keys": "required"})
    preset = preset_arg(data)
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        raise APIError(422, "validation_failed", "filters must be an object.", fields={"filters": "object"})
    args = MultiDict()
    for k, v in filters.items():
        for item in (v if isinstance(v, list) else [v]):
            args.add(k, str(item))
    if args.get("added_from") or args.get("added_to"):
        args["added"], args["from"], args["to"] = "custom", args.get("added_from", ""), args.get("added_to", "")
    for f in folders:
        if f.split("/", 1)[0] not in enc.ROOT_CANDIDATES or any(p in ("", ".", "..") for p in f.split("/")):
            raise APIError(422, "validation_failed", f"Unknown folder {f}.", fields={"folders": f})
    picked, unknown, bad = ui.select_files(keys, folders, ui.file_filters(args), statuses=("offer",))
    rejected = [(k, "unknown_key") for k in unknown] + [(f, "no_matching_files") for f in bad]
    rows = file_rows(picked)
    pending = {i["key"]: i["state"] for i in enc.queue_items() if i["state"] in ("waiting", "encoding")}
    replacing = enc.pending_replaces()
    skipped, wanted = [], []
    for k in picked:
        st = rows.get(k, {}).get("status")
        reason = ("encoding" if pending.get(k) == "encoding" else "already_queued" if k in pending else
                  "replace_pending" if k in replacing else "already_optimized" if st == "h265" else
                  "awaiting_review" if st == "waiting" else None)
        (skipped.append((k, reason)) if reason else wanted.append(k))
    done = enc.write_batch(wanted, preset) if wanted else []
    skipped += [(k, "already_queued") for k in wanted if k not in set(done)]
    if done:
        audit("queued" + (f" with preset {presets.name_of(preset)}" if preset else ""), len(done))
    return outcome(done, skipped, rejected)


@route("POST", "/queue/remove", "queue:write", "Remove waiting entries from the queue",
       params=[("keys", "body: list", "File keys"), ("all", "body: true", "Every waiting entry")],
       response={"done": ["share2/…S02E01.mkv"], "skipped": [], "rejected": []})
@idempotent
def queue_remove():
    data = body()
    if data.get("all") is True:
        removed = enc.remove_queued(None)
        keys = []
    else:
        keys = str_list(data, "keys", required=True)
        removed = enc.remove_queued(keys)
    removed = list(removed)
    if removed:
        audit("removed from the queue", len(removed))
    return outcome(removed, [(k, "not_waiting") for k in keys if k not in set(removed)])


@route("POST", "/queue/move", "queue:write", "Reorder waiting entries",
       params=[("keys", "body: list", "File keys"), ("op", "body: string", "top, bottom, up, down"),
               ("before", "body: string", "Instead of op: put the keys in front of this key (\"\" = at the end)")],
       response={"order": ["share2/…S02E03.mkv", "share2/…S02E01.mkv"], "moved": 1})
@idempotent
def queue_move():
    data = body()
    keys = str_list(data, "keys", required=True)
    if "before" in data:
        op, target = "before", data.get("before") or ""
        if not isinstance(target, str):
            raise APIError(422, "validation_failed", "before must be a key or \"\".", fields={"before": "string"})
    else:
        op, target = data.get("op"), ""
        if op not in ("top", "bottom", "up", "down"):
            raise APIError(422, "validation_failed", "op must be top, bottom, up or down.", fields={"op": "invalid"})
    waiting = [i["key"] for i in enc.queue_items() if i["state"] == "waiting"]
    moved = [k for k in keys if k in waiting]
    if not moved:
        raise APIError(409, "conflict", "None of these entries is waiting.", reason="not_waiting")
    n = enc.reorder_queued(enc.moved_order(waiting, op, moved, target))
    if n:
        audit("reordered the queue", len(moved))
    return {"order": [i["key"] for i in enc.queue_items() if i["state"] == "waiting"], "moved": len(moved) if n else 0}


@route("POST", "/queue/retry", "queue:write", "Queue failed or stopped files again (with their last preset)",
       params=[("keys", "body: list", "File keys"), ("failed", "body: true", "Every file whose last result is FAIL or STOPPED")],
       response=BULK)
@idempotent
def queue_retry():
    data = body()
    if data.get("failed") is True:
        keys = [h["key"] for h in enc.history_entries() if h["can_retry"]]
    else:
        keys = str_list(data, "keys", required=True)
    queued, _ = enc.requeue(keys) if keys else ([], 0)
    queued = list(queued)
    if queued:
        audit("queued again", len(queued))
    return outcome(queued, [(k, "not_failed_or_pending") for k in keys if k not in set(queued)])


@route("POST", "/encoding/stop", "queue:write", "Stop the current encode (skip: go on with the next file; all: pause the queue)",
       params=[("mode", "body: string", "skip or all"), ("key", "body: string", "The key of the file being encoded (required)")],
       response={"stopping": "share2/…S02E03.mkv", "mode": "skip"})
@idempotent
def encoding_stop():
    data = body()
    mode, key = data.get("mode"), data.get("key")
    if mode not in ("skip", "all"):
        raise APIError(422, "validation_failed", "mode must be skip or all.", fields={"mode": "invalid"})
    cur = enc.current_progress()
    if not cur:
        raise APIError(409, "conflict", "Nothing is being encoded.", reason="not_encoding")
    if key != cur["key"]:
        raise APIError(409, "conflict", "That file isn't the one being encoded.", reason="not_current", current=cur["key"])
    if not enc.worker_can_stop():
        raise APIError(409, "conflict", "The running worker is too old to stop an encode; wait until its run ends.",
                       reason="not_stoppable")
    problem = enc.request_stop(mode, key)
    if problem:
        raise APIError(409, "conflict", problem, reason="not_stoppable")
    audit("stopped the encode" + (" and paused the queue" if mode == "all" else ""))
    return {"stopping": key, "mode": mode}


@route("POST", "/encoding/resume", "queue:write", "End the pause after stopping everything",
       response={"paused": False})
@idempotent
def encoding_resume():
    body()
    was = enc.is_paused()
    enc.resume_queue()
    if was:
        audit("resumed the queue")
    return {"paused": False}


@route("POST", "/scans", "library:scan", "Start a scan: everything, one share or one folder",
       params=[("share", "body: integer", "Share number"), ("path", "body: string", "share<N>/<folder or file>"),
               ("force", "body: boolean", "Read every file with ffprobe again")],
       response={"scan": {"running": True, "label": "Scan of all shares", "started": "2026-11-06T09:00:00+01:00"}})
@idempotent
def scan_start():
    data = body()
    if g.get("config_error"):
        raise APIError(409, "conflict", "The configuration is incomplete.", reason="config_error")
    if ui.SCAN["running"] or enc.scan_progress():
        raise APIError(409, "conflict", "A scan is already running.", reason="scan_running")
    force = data.get("force") is True
    offline = set(ui.offline_now())
    if data.get("path"):
        path = str(data["path"]).strip().strip("/")
        if path.split("/", 1)[0] not in enc.ROOT_CANDIDATES or any(p in ("", ".", "..") for p in path.split("/")):
            raise APIError(422, "validation_failed", "Unknown file or folder.", fields={"path": "unknown"})
        if enc.SHARE_NAMES.get(path.split("/", 1)[0]) in offline:
            raise APIError(409, "conflict", "The share can't be reached.", reason="share_offline")
        label = f"Check of {enc.short(path) if '/' in path else enc.SHARE_NAMES[path]}"
        thread = ui.start_background_scan(label, force=True, path=path)
    elif data.get("share") is not None:
        prefix = f"share{data['share']}"
        if not isinstance(data["share"], int) or prefix not in enc.ROOT_CANDIDATES:
            raise APIError(422, "validation_failed", "Unknown share.", fields={"share": "unknown"})
        if enc.SHARE_NAMES.get(prefix) in offline:
            raise APIError(409, "conflict", "The share can't be reached.", reason="share_offline")
        label = f"{'Full rescan' if force else 'Scan'} of {enc.SHARE_NAMES.get(prefix, prefix)}"
        thread = ui.start_background_scan(label, [prefix], force)
    else:
        label = "Scan of all shares"
        thread = ui.start_background_scan(label, None, force)
    if thread is None:
        raise APIError(409, "conflict", "A scan is already running.", reason="scan_running")
    audit(label.lower() + (" (full)" if force and not data.get("path") else ""))
    resp = ui.app.make_response({"scan": {"running": True, "label": label, "started": iso(ui.SCAN.get("started"))}})
    resp.status_code = 202
    return resp


@route("POST", "/encodes/delete", "review:write", "Delete finished encodes from the output folder (optionally queue the files again)",
       params=[("keys", "body: list", "File keys"), ("requeue", "body: boolean", "Queue the files again"),
               ("preset", "body: string", "With requeue: preset id; null = the share's")],
       response=dict(BULK, freed_bytes=734003200, queued=[]))
@idempotent
def encodes_delete():
    data = body()
    keys = str_list(data, "keys", required=True)
    requeue = data.get("requeue") is True
    preset = preset_arg(data) if requeue else None
    res = ui.delete_encodes(keys, requeue, preset)
    if res["deleted"]:
        audit("deleted encodes" + (" and queued them again" if requeue else ""), len(res["deleted"]))
    rejected = [(k, r) for k, r in res["skipped"] if r == "unknown_key"]
    skipped = [(k, r) for k, r in res["skipped"] if r != "unknown_key"]
    return outcome(res["deleted"], skipped, rejected, freed_bytes=res["freed"], queued=res["queued"])


@route("POST", "/history/dismiss", "review:write", "Hide History entries (nothing on disk changes)",
       params=[("keys", "body: list", "File keys"), ("older_than_days", "body: integer", "Every entry older than this"),
               ("all", "body: true", "Every entry History shows"), ("status", "body: string", "With all: only this result")],
       response={"hidden": 12})
@idempotent
def history_dismiss():
    data = body()
    if isinstance(data.get("older_than_days"), int) and data["older_than_days"] >= 0:
        rows = enc.history_entries(data.get("status") or "", older_than=data["older_than_days"])
        upto = {}
        for r in rows:
            upto[r["key"]] = max(upto.get(r["key"], ""), r["at"])
        enc.dismiss(upto, upto)
        n = len(upto)
    elif data.get("all") is True:
        status = data.get("status") or ""
        keys = list({h["key"] for h in enc.history_entries(result=status)})
        if keys:
            enc.dismiss(keys)
        n = len(keys)
    else:
        keys = str_list(data, "keys", required=True)
        enc.dismiss(keys)
        n = len(keys)
    if n:
        audit("hid History entries", n)
    return {"hidden": n}


# ─── Phase 3 (opt-in scopes): replace, kept originals ─────────────────────────

@route("POST", "/replace", "replace:write", "Replace originals with their finished encodes (all checked before any starts)",
       params=[("items", "body: list", "[{key, new_name (null = suggested), keep_original (default: setting)}], at most 500")],
       response={"jobs": [{"key": "share2/…S02E01.mkv", "id": "20261106-090000-123456", "new_name": "…x265.mkv"}]})
@idempotent
def replace_start():
    data = body()
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > ui.BULK_REPLACE_MAX:
        raise APIError(422, "validation_failed", f"items must be a list of 1–{ui.BULK_REPLACE_MAX} objects.",
                       fields={"items": "invalid"})
    default_keep = (enc.CONFIG.get("KEEP_ORIGINALS") or "1").strip() != "0"
    pairs, keeps = [], []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("key"), str):
            raise APIError(422, "validation_failed", "Every item needs a key.", fields={"items": "key"})
        name = it.get("new_name")
        if name is None:
            name = suggested_name(it["key"])
        if not isinstance(name, str) or not name.strip():
            name = ""
        pairs.append((it["key"], name.strip()))
        keep = it.get("keep_original", default_keep)
        keeps.append(keep if isinstance(keep, bool) else default_keep)
    errors, jobs = ui.check_replaces(pairs)
    if errors:
        raise APIError(422, "validation_failed", "Nothing was started; fix these items.",
                       items=[{"key": k, "reason": v} for k, v in errors.items()])
    out = []
    for (key, enc_name, name), keep in zip(jobs, keeps):
        out.append({"key": key, "id": enc.request_replace(key, enc_name, name, keep), "new_name": name,
                    "keep_original": keep})
    audit("started replaces", len(out))
    resp = ui.app.make_response({"jobs": out})
    resp.status_code = 202
    return resp


def suggested_name(key):
    enc_dir = enc.first_existing(enc.ENCODED_CANDIDATES)
    encoded = enc.encoded_files(enc_dir).get(Path(key).stem.lower()) if enc_dir else None
    if not encoded:
        return None
    codec = enc.probe(enc_dir / encoded)["video_codec"] if enc.FFPROBE else None
    return enc.suggest_name(key.rsplit("/", 1)[-1], encoded, codec, enc.encode_quality(key))


@route("GET", "/replace/suggest", "review:read", "The file name Replace… would suggest for a file",
       params=[("key", "string", "The file's key")], response={"key": "share2/…S02E01.mkv", "name": "…x265-NTB.rf20.mkv"})
def replace_suggest():
    key = request.args.get("key", "")
    if key not in file_rows([key]):
        raise APIError(404, "not_found", "No file with this key.")
    name = suggested_name(key)
    if not name:
        raise APIError(404, "not_found", "No finished encode of this file in the output folder.")
    return {"key": key, "name": name}


@route("GET", "/replace/progress", "review:read", "The replace progress bar's data: jobs of the current wave by stage",
       response={"active": True, "waiting": 1, "moving": 0, "checking": 0, "done": 3, "failed": 0, "cancelled": 0,
                 "items": [{"id": "20261106-090000-123456", "name": "…", "stage": "waiting"}]})
def replace_progress():
    rp = ui.replace_progress()
    return {"active": rp.get("active", False), **{k: rp.get(k, 0) for k in ("waiting", "moving", "checking", "done", "failed", "cancelled")},
            "items": [{"id": i["id"], "name": i["name"], "stage": i["stage"]} for i in rp.get("items", [])]}


@route("DELETE", "/replace/{job_id}", "replace:write", "Cancel a replace the worker hasn't started",
       response={"cancelled": "20261106-090000-123456"})
def replace_cancel(job_id):
    if not re.fullmatch(r"[\d-]{6,40}", job_id):
        raise APIError(404, "not_found", "Unknown job.")
    st = enc.replace_status(job_id)
    if st.get("message") == "Unknown job.":
        raise APIError(404, "not_found", "Unknown job.")
    done, late = enc.cancel_replaces(ids=[job_id])
    if not done:
        raise APIError(409, "conflict", "The worker has already started this replace, or it is finished.", reason="already_started")
    audit("cancelled a replace")
    return {"cancelled": job_id}


def original_obj(o, files):
    return {"path": o["key"], "size": o["size"], "kept_since": iso(o.get("kept")),
            "replaced_by": files.get(o.get("new_key")) if o.get("new_key") else None,
            "warning": o.get("warn") or None, "saved_bytes": o.get("saved")}


@route("GET", "/originals", "originals:read", "The originals Replace… kept (paths inside .replaced/)",
       params=PAGING, response={"items": [{"path": "Northwind Station (2018)/Season 02/…S02E01.mkv", "size": 2147483648,
                                           "kept_since": "2026-11-05T14:00:00+01:00", "replaced_by": FILE_EXAMPLE,
                                           "warning": None, "saved_bytes": 1413480448}], "total": 1, "next": None})
def originals():
    rows = [dict(r) for r in ui.kept_list()]
    ui.kept_info(rows)                          # adds the replacement's state as warn
    files = files_for([r["new_key"] for r in rows if r.get("new_key")])
    return page([original_obj(r, files) for r in rows])


@route("POST", "/originals/delete", "originals:write", "Delete kept originals permanently",
       params=[("paths", "body: list", "Paths inside .replaced/"), ("older_than_days", "body: integer", "Every original kept longer"),
               ("force", "body: boolean", "With paths: also when the replacement is missing or offline")],
       response=dict(BULK, freed_bytes=24696061952))
@idempotent
def originals_delete():
    data = body()
    if isinstance(data.get("older_than_days"), int):
        if data["older_than_days"] < 1:
            raise APIError(422, "validation_failed", "older_than_days must be at least 1.", fields={"older_than_days": "min 1"})
        count, freed, held = enc.purge_kept(data["older_than_days"])
        ui._KEPT_GEN[0] += 1
        if count:
            audit("deleted kept originals", count)
        return {"done": count, "skipped": held, "rejected": [], "freed_bytes": freed}
    paths = str_list(data, "paths", required=True)
    kept = {o["key"]: o for o in enc.kept_originals()}
    rejected = [(p, "not_found") for p in paths if p not in kept]
    ok, held = enc.split_kept([kept[p] for p in paths if p in kept], force=data.get("force") is True)
    count, freed = enc.delete_kept([o["key"] for o in ok])
    ui._KEPT_GEN[0] += 1
    if count:
        audit("deleted kept originals", count)
    return outcome([o["key"] for o in ok], [(o["key"], "replacement_missing_or_unconfirmed") for o in held], rejected,
                   freed_bytes=freed)
