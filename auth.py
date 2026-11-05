"""
Login of the web UI: the account, password hashes, the one-time setup code and the throttle
for failed attempts. Used by webui.py (the login pages and the check before every request) and
by `python3 webui.py --set-password`.

The account lives in auth.json in the data folder (not in config.env: zsh sources that file and
a hash holds `$` signs):

    {"user": "admin", "hash": "scrypt:…", "generation": 3, "changed": "2026-11-05 14:00",
     "oidc_ok": {"issuer": "https://id.example.com", "client_id": "…"}}

`generation` is stored in every session cookie at login; raising it (new password, "Sign out
everywhere") ends every session that carries an older one. `oidc_ok` records the provider and
client of the last successful single sign-on: only a sign-on that worked lets the password login be
turned off. With single sign-on alone the file may hold no account (no user and hash). While the
password login is on and there is no account, the web UI only shows the setup page, which asks for
the setup code in `.setup-code` (also printed to the web UI's log at startup), so nobody who
merely reaches the page can claim the install.
"""

import hashlib
import json
import os
import secrets
import sys
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

import encoding as enc

AUTH_FILE = enc.DATA_DIR / "auth.json"
SETUP_CODE_FILE = enc.DATA_DIR / ".setup-code"

PASSWORD_MIN = 8
PASSWORD_MAX = 256                              # hashing very long input costs time for nothing
USER_MAX = 64

# Apple's /usr/bin/python3 is built without scrypt; PBKDF2 is the fallback there
METHOD = "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2:sha256:600000"
# Checked when the user name is unknown, so the answer takes as long as for a wrong password
_DUMMY = generate_password_hash(secrets.token_hex(16), method=METHOD)

_LOCK = threading.Lock()
_CACHE = [None, {}]                             # [(mtime_ns, size), file content]


def _data():
    """Everything in auth.json ({} without it). Cached until the file changes."""
    try:
        st = AUTH_FILE.stat()
    except FileNotFoundError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    if _CACHE[0] != stamp:
        try:
            data = json.loads(AUTH_FILE.read_text())
        except (OSError, ValueError):
            data = {}
        _CACHE[:] = [stamp, data if isinstance(data, dict) else {}]
    return _CACHE[1]


def account():
    """The password account ({"user", "hash", "generation", …}) or None."""
    data = _data()
    return data if data.get("user") and data.get("hash") else None


def generation():
    """The current session generation (sessions with another one are signed out)."""
    try:
        return int(_data().get("generation", 1))
    except (TypeError, ValueError):
        return 1


def oidc_verified(issuer, client_id):
    """Whether a single sign-on with this provider and client has worked."""
    ok = _data().get("oidc_ok") or {}
    return bool(issuer) and ok.get("issuer") == issuer.rstrip("/") and ok.get("client_id") == client_id


def record_oidc(issuer, client_id):
    with _LOCK:
        data = dict(_data())
        mark = {"issuer": issuer.rstrip("/"), "client_id": client_id}
        if data.get("oidc_ok") != mark:
            data["oidc_ok"] = mark
            data.setdefault("generation", 1)
            _write(data)


def _write(data):
    """Atomic write; the file is only ever readable by the user the web UI runs as."""
    enc.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_name("." + AUTH_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, AUTH_FILE)


def user_problem(name):
    name = (name or "").strip()
    if not name:
        return "Enter a user name."
    if len(name) > USER_MAX:
        return f"The user name can have at most {USER_MAX} characters."
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "The user name can't contain control characters."
    return None


def password_problem(password, confirm=None):
    if len(password or "") < PASSWORD_MIN:
        return f"The password needs at least {PASSWORD_MIN} characters (longer is better, e.g. a few random words)."
    if len(password) > PASSWORD_MAX:
        return f"The password can have at most {PASSWORD_MAX} characters."
    if confirm is not None and password != confirm:
        return "The two passwords don't match."
    return None


def set_account(user, password=None):
    """Create the account or change its name and/or password. A new password raises the
    generation, which signs out every session. Returns the new generation."""
    with _LOCK:
        old = _data()
        data = dict(old, user=user.strip())
        if password is not None:
            data["hash"] = generate_password_hash(password, method=METHOD)
            data["generation"] = int(old.get("generation", 0)) + 1
            data["changed"] = enc.now_str()
        data.setdefault("generation", 1)
        _write(data)
        clear_setup_code()
        return data["generation"]


def bump_generation():
    """Sign out every session; returns the new generation (the caller keeps its own session)."""
    with _LOCK:
        data = dict(_data())
        data["generation"] = int(data.get("generation", 1)) + 1
        _write(data)
        return data["generation"]


def verify(user, password):
    """The account if user and password match, else None (same time either way)."""
    acc = account()
    password = (password or "")[:PASSWORD_MAX + 1]
    if not acc or not secrets.compare_digest((user or "").strip().encode(), acc["user"].encode()):
        check_password_hash(_DUMMY, password)
        return None
    return acc if _matches(acc["hash"], password) else None


def _matches(stored, password):
    try:
        return check_password_hash(stored, password)
    except (ValueError, AttributeError) as e:   # e.g. an scrypt hash on a Python built without scrypt
        print(f"The stored password hash can't be checked here ({e}). Set the password again with "
              "python3 webui.py --set-password.", file=sys.stderr, flush=True)
        return False


def check_password(password):
    acc = account()
    return bool(acc) and _matches(acc["hash"], (password or "")[:PASSWORD_MAX + 1])


def needs_rehash():
    """True if the stored hash uses another method than this Python offers (e.g. created with
    PBKDF2 on a Python without scrypt): the next login stores it again."""
    acc = account()
    return bool(acc) and not acc["hash"].startswith(METHOD.split(":")[0] + ":")


def rehash(password):
    with _LOCK:
        data = dict(_data())
        if data.get("hash"):
            data["hash"] = generate_password_hash(password, method=METHOD)
            _write(data)


# ─── Setup code ───────────────────────────────────────────────────────────────

def setup_code():
    """The one-time code that unlocks the setup page while there is no account; created once and
    kept (restarts print the same code) until the account exists."""
    with _LOCK:
        try:
            code = SETUP_CODE_FILE.read_text().strip()
        except FileNotFoundError:
            code = ""
        if len(code) < 8:
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O, 1/I
            code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
            enc.DATA_DIR.mkdir(parents=True, exist_ok=True)
            fd = os.open(SETUP_CODE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(code + "\n")
        return code


def setup_code_ok(entered):
    norm = lambda s: "".join(ch for ch in (s or "").upper() if ch.isalnum())
    return secrets.compare_digest(norm(entered).encode(), norm(setup_code()).encode())


def clear_setup_code():
    try:
        SETUP_CODE_FILE.unlink()
    except FileNotFoundError:
        pass


# ─── Throttle ─────────────────────────────────────────────────────────────────
# Failed logins and setup codes per client address: after FREE failures within WINDOW seconds,
# each further attempt waits twice as long as the last (up to MAX_WAIT). Kept in memory; a
# restart forgets it, which is fine for slowing down guessing.
FREE = 5
WINDOW = 15 * 60
MAX_WAIT = 15 * 60
_FAILS = {}                                     # address -> [count, first failure, blocked until]


def wait_seconds(addr):
    """Seconds this address has to wait before the next attempt (0 = go ahead)."""
    with _LOCK:
        entry = _FAILS.get(addr)
        if not entry:
            return 0
        now = time.time()
        if now - entry[1] > WINDOW and now >= entry[2]:
            del _FAILS[addr]
            return 0
        return max(0, int(entry[2] - now + 0.999))


def failed(addr):
    with _LOCK:
        now = time.time()
        entry = _FAILS.get(addr)
        if not entry or (now - entry[1] > WINDOW and now >= entry[2]):
            entry = _FAILS[addr] = [0, now, 0]
        entry[0] += 1
        if entry[0] >= FREE:
            entry[2] = now + min(MAX_WAIT, 2 ** (entry[0] - FREE) * 30)
        if len(_FAILS) > 10000:                 # don't grow without bound
            for k in [k for k, v in _FAILS.items() if now - v[1] > WINDOW and now >= v[2]]:
                del _FAILS[k]


def succeeded(addr):
    with _LOCK:
        _FAILS.pop(addr, None)


def describe_wait(seconds):
    return f"{seconds} seconds" if seconds < 90 else f"{(seconds + 59) // 60} minutes"
