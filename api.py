"""
The Handlebreaks API, version 1 (phase 1: reading). Specification: docs/api-v1-spec.md and the
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
    return error_response(e.status, e.code, e.message, **e.details)


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
           "offline": ui.offline_now(), "scanning": bool(ui.SCAN["running"])}
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
                 "offline": [], "scan": {"running": False, "label": None, "started": None, "last_finished": None},
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
            "scan": {"running": bool(ui.SCAN["running"]), "label": ui.SCAN.get("label") if ui.SCAN["running"] else None,
                     "started": iso(ui.SCAN.get("started")) if ui.SCAN["running"] else None,
                     "last_finished": iso(enc.last_scan())},
            "replaces": {k: rp.get(k, 0) for k in ("waiting", "moving", "checking")}}


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
