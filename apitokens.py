"""
API tokens of Handlebreaks (see docs/api-v1-spec.md, section 2): `hb_<id>_<secret>`, created in
Settings → API or with `python3 webui.py --create-token`, shown once. Only the SHA-256 of the
whole token is stored, in api-tokens.json in the data folder (0600, written via temp file and
rename):

    {"tokens": [{"id", "name", "hash", "scopes", "allowed", "created", "created_by", "expires",
                 "last_used", "last_address", "revoked"}]}

Times are ISO 8601 with offset. Revoked tokens stay listed for 30 days, then they are dropped.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta

import encoding as enc

TOKENS_FILE = enc.DATA_DIR / "api-tokens.json"

# scope -> (phase, description). The order is the order of the token form.
SCOPES = {
    "status:read": (1, "Current encode, worker state, offline shares, scans; the dashboard summary"),
    "queue:read": (1, "The queue with order, preset and state"),
    "library:read": (1, "Shares, folders and files with their codec, resolution, bitrate and status"),
    "review:read": (1, "Encodes awaiting review, the history, the state of replace jobs"),
    "presets:read": (1, "Presets with all their settings"),
    "stats:read": (1, "Statistics of the library and of the space saved"),
    "events:read": (1, "The notification feed"),
    "system:read": (1, "Versions, HandBrakeCLI, free space in the output folder (no paths or settings)"),
    "logs:read": (1, "HandBrake's logs of failed encodes"),
    "originals:read": (3, "The originals Replace… kept"),
    "queue:write": (2, "Queue files and folders, remove, reorder, queue failed files again, stop and resume encoding"),
    "library:scan": (2, "Start scans of everything, a share or a folder"),
    "review:write": (2, "Delete finished encodes (optionally queue them again), hide History entries"),
    "replace:write": (3, "Replace originals in the library with their encodes, cancel waiting replaces"),
    "originals:write": (3, "Delete kept originals permanently"),
}
OPT_IN = {"replace:write", "originals:write"}   # change or delete files in the library: never in a bundle
BUNDLES = {
    "dashboard": ("Dashboard (read-only)", ["status:read", "queue:read", "review:read", "stats:read", "system:read"]),
    "read": ("Read everything", [s for s in SCOPES if s.endswith(":read") and s != "originals:read"]),
    "automation": ("Automation", [s for s in SCOPES if s.endswith(":read") and s != "originals:read"]
                   + ["queue:write", "library:scan", "review:write"]),
}
EXPIRY_DAYS = (30, 90, 365, 0)                  # 0 = never
DEFAULT_EXPIRY = 90
MAX_TOKENS = 50
NAME_MAX = 64
TOKEN_RE = re.compile(r"hb_([a-z2-7]{8})_([A-Za-z0-9_-]{43})")
LAST_USED_EVERY = 60                            # seconds between writes of last_used per token

_LOCK = threading.Lock()
_CACHE = [None, {"tokens": []}]


def now():
    return datetime.now().astimezone().replace(microsecond=0)


def parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _data():
    try:
        st = TOKENS_FILE.stat()
    except FileNotFoundError:
        return {"tokens": []}
    stamp = (st.st_mtime_ns, st.st_size)
    if _CACHE[0] != stamp:
        try:
            data = json.loads(TOKENS_FILE.read_text())
            data = data if isinstance(data, dict) and isinstance(data.get("tokens"), list) else {"tokens": []}
        except (OSError, ValueError):
            data = {"tokens": []}
        _CACHE[:] = [stamp, data]
    return _CACHE[1]


def _write(data):
    enc.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_FILE.with_name("." + TOKENS_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, TOKENS_FILE)


def _hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def state(t, at=None):
    """active, expired or revoked."""
    if t.get("revoked"):
        return "revoked"
    exp = parse_time(t.get("expires"))
    return "expired" if exp and exp <= (at or now()) else "active"


def tokens():
    """Every token (without its hash), newest first, revoked ones of the last 30 days included."""
    out = [{k: v for k, v in t.items() if k != "hash"} | {"state": state(t)} for t in _data()["tokens"]]
    return sorted(out, key=lambda t: t.get("created") or "", reverse=True)


def name_problem(name, ignore_id=None):
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX:
        return f"Give the token a name (1–{NAME_MAX} characters)."
    if any(ord(c) < 32 for c in name):
        return "The name can't contain control characters."
    if any(t["name"].casefold() == name.casefold() and t["id"] != ignore_id and state(t) == "active"
           for t in _data()["tokens"]):
        return "Another active token has this name."
    return None


def parse_allowed(text):
    """Allowed networks from text (addresses or CIDR ranges separated by spaces, commas or
    lines) -> (list of normalized networks, problem or None)."""
    nets = []
    for part in re.split(r"[\s,]+", text or ""):
        if not part:
            continue
        try:
            nets.append(str(ipaddress.ip_network(part, strict=False)))
        except ValueError:
            return [], f"{part} isn't an address or network (e.g. 192.168.1.20 or 192.168.1.0/24)."
    return list(dict.fromkeys(nets)), None


def address_allowed(t, address):
    if not t.get("allowed"):
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in ipaddress.ip_network(n) for n in t["allowed"])


def create(name, scopes, expires_days=DEFAULT_EXPIRY, allowed=(), created_by="", opt_in=False):
    """A new token: returns (the full token, shown only now, and its record). Raises ValueError
    with a message for invalid input. Scopes in OPT_IN need opt_in=True (an extra confirmation)."""
    problem = name_problem(name)
    if problem:
        raise ValueError(problem)
    scopes = [s for s in SCOPES if s in set(scopes)]
    if not scopes:
        raise ValueError("Choose at least one scope.")
    if OPT_IN & set(scopes) and not opt_in:
        raise ValueError("Scopes that change files in the library (" + ", ".join(sorted(OPT_IN & set(scopes)))
                         + ") need the extra confirmation.")
    if expires_days not in EXPIRY_DAYS:
        raise ValueError("Choose an expiry of 30, 90 or 365 days, or never.")
    with _LOCK:
        data = {"tokens": [t for t in _data()["tokens"] if not _stale(t)]}
        if sum(state(t) == "active" for t in data["tokens"]) >= MAX_TOKENS:
            raise ValueError(f"At most {MAX_TOKENS} active tokens; revoke one first.")
        ids = {t["id"] for t in data["tokens"]}
        while True:
            tid = base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()
            if tid not in ids:
                break
        token = f"hb_{tid}_{secrets.token_urlsafe(32)}"
        created = now()
        rec = {"id": tid, "name": name.strip(), "hash": _hash(token), "scopes": scopes, "allowed": list(allowed),
               "created": created.isoformat(), "created_by": created_by,
               "expires": (created + timedelta(days=expires_days)).isoformat() if expires_days else None,
               "last_used": None, "last_address": None, "revoked": None}
        data["tokens"].append(rec)
        _write(data)
    return token, {k: v for k, v in rec.items() if k != "hash"} | {"state": "active"}


def _stale(t):
    rev = parse_time(t.get("revoked"))
    return bool(rev) and now() - rev > timedelta(days=30)


def update(tid, name=None, allowed=None):
    with _LOCK:
        data = _data()
        for t in data["tokens"]:
            if t["id"] == tid and state(t) != "revoked":
                if name is not None:
                    problem = name_problem(name, ignore_id=tid)
                    if problem:
                        raise ValueError(problem)
                    t["name"] = name.strip()
                if allowed is not None:
                    t["allowed"] = list(allowed)
                _write(data)
                return True
    return False


def revoke(tid=None, every=False):
    """Revoke one token (by id) or all; returns how many were revoked."""
    with _LOCK:
        data = _data()
        n = 0
        stamp = now().isoformat()
        for t in data["tokens"]:
            if (every or t["id"] == tid) and not t.get("revoked"):
                t["revoked"] = stamp
                n += 1
        if n:
            _write(data)
        return n


def verify(header_value):
    """The token record for an Authorization header value ("Bearer hb_…"), or None for any
    problem (missing, malformed, unknown, wrong secret, expired, revoked). Always hashes, so the
    answer takes about as long whatever is wrong."""
    value = (header_value or "").strip()
    token = value[7:].strip() if value[:7].lower() == "bearer " else ""
    digest = _hash(token if token.isascii() else "")
    m = TOKEN_RE.fullmatch(token)
    if not m:
        return None
    for t in _data()["tokens"]:
        if t["id"] == m.group(1):
            if hmac.compare_digest(t["hash"], digest) and state(t) == "active":
                return t
            return None
    return None


def touch(tid, address):
    """Record the last use (at most every LAST_USED_EVERY seconds per token)."""
    stamp = now()
    with _LOCK:
        data = _data()
        for t in data["tokens"]:
            if t["id"] == tid:
                last = parse_time(t.get("last_used"))
                if last and (stamp - last).total_seconds() < LAST_USED_EVERY and t.get("last_address") == address:
                    return
                t["last_used"], t["last_address"] = stamp.isoformat(), address
                _write(data)
                return


def expiring(days=14):
    """Active tokens that expire within `days`."""
    limit = now() + timedelta(days=days)
    return [t for t in tokens() if t["state"] == "active" and t.get("expires") and parse_time(t["expires"]) <= limit]


def warn_expiring(days=7):
    """Active tokens expiring within `days` that weren't warned about yet; marks them warned."""
    limit = now() + timedelta(days=days)
    out = []
    with _LOCK:
        data = _data()
        for t in data["tokens"]:
            exp = parse_time(t.get("expires"))
            if state(t) == "active" and exp and exp <= limit and not t.get("warned"):
                t["warned"] = now().isoformat()
                out.append(dict(t))
        if out:
            _write(data)
    return out
