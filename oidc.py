"""
Single sign-on for the web UI with OpenID Connect (e.g. Pocket ID, Authelia, Authentik,
Keycloak), standard library only.

Flow: authorization code with PKCE (S256), `state` (bound to the browser's session) and `nonce`,
as OpenID Connect Core 1.0 and the OAuth 2.0 Security Best Current Practice (RFC 9700) describe it
for a confidential or public web client. The ID token comes straight from the provider's token
endpoint over HTTPS with a verified certificate, so its issuer is established by TLS (OIDC Core
3.1.3.7, step 6) and its signature isn't checked here; its claims are: `iss`, `aud`/`azp`, `exp`,
`iat` and `nonce`. Who may sign in is decided by the web UI (AUTH_OIDC_ALLOWED_USERS /
AUTH_OIDC_ALLOWED_GROUPS), not by the provider alone.

The client secret is only ever sent to the provider's token endpoint, never logged or shown.
"""

import base64
import hashlib
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 10
DISCOVERY_TTL = 3600                            # seconds a provider's discovery document is reused
PENDING_TTL = 600                               # seconds a sign-in may take at the provider
CLOCK_SKEW = 120                                # seconds of clock difference accepted for exp/iat

_LOCK = threading.Lock()
_DISCOVERY = {}                                 # issuer -> (fetched at, document)
_PENDING = {}                                   # state -> {verifier, nonce, next, created, issuer, client_id}


class OIDCError(Exception):
    """A problem to show to the user (never contains the client secret)."""


def issuer_problem(issuer):
    """Why this issuer URL can't be used, or None. HTTPS only, except on this machine."""
    if not issuer:
        return "Enter the issuer URL of your provider."
    u = urllib.parse.urlsplit(issuer)
    if u.scheme not in ("https", "http") or not u.netloc:
        return "The issuer must be a URL such as https://id.example.com."
    if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        return "The issuer must use https (http only works for a provider on this machine)."
    if u.query or u.fragment:
        return "The issuer URL can't have a query or fragment."
    return None


def _request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            body = {}
        return e.code, body
    except ssl.SSLError as e:
        raise OIDCError(f"The provider's certificate isn't trusted ({e.reason or e}).")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise OIDCError(f"The provider can't be reached ({reason}).")
    except ValueError:
        raise OIDCError("The provider didn't answer with JSON.")


def discover(issuer, fresh=False):
    """The provider's metadata (/.well-known/openid-configuration), checked and cached."""
    problem = issuer_problem(issuer)
    if problem:
        raise OIDCError(problem)
    issuer = issuer.rstrip("/")
    with _LOCK:
        hit = _DISCOVERY.get(issuer)
    if hit and not fresh and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    status, doc = _request(issuer + "/.well-known/openid-configuration")
    if status != 200 or not isinstance(doc, dict):
        raise OIDCError(f"No OpenID Connect configuration at {issuer}/.well-known/openid-configuration (HTTP {status}).")
    if doc.get("issuer", "").rstrip("/") != issuer:
        raise OIDCError(f"The provider calls itself {doc.get('issuer')!r}, not {issuer!r}: use exactly that as issuer URL.")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not doc.get(key):
            raise OIDCError(f"The provider's configuration has no {key}.")
        u = urllib.parse.urlsplit(doc[key])
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")):
            raise OIDCError(f"The provider's {key} doesn't use https.")
    if "code" not in doc.get("response_types_supported", ["code"]):
        raise OIDCError("The provider doesn't support the authorization code flow.")
    methods = doc.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise OIDCError("The provider doesn't support PKCE with S256.")
    with _LOCK:
        _DISCOVERY[issuer] = (time.time(), doc)
    return doc


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start(cfg, redirect_uri, next_url):
    """(state, URL of the provider's sign-in page) for a new sign-in."""
    doc = discover(cfg["issuer"])
    verifier = secrets.token_urlsafe(64)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _PENDING.items() if now - v["created"] > PENDING_TTL]:
            del _PENDING[k]
        if len(_PENDING) > 1000:                # don't grow without bound
            raise OIDCError("Too many sign-ins in progress. Try again in a few minutes.")
        _PENDING[state] = {"verifier": verifier, "nonce": nonce, "next": next_url, "created": now,
                           "issuer": cfg["issuer"].rstrip("/"), "client_id": cfg["client_id"], "redirect_uri": redirect_uri}
    params = {"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri,
              "scope": cfg["scopes"], "state": state, "nonce": nonce,
              "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()), "code_challenge_method": "S256"}
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return state, doc["authorization_endpoint"] + sep + urllib.parse.urlencode(params)


def _token_request(doc, cfg, form):
    """POST to the token endpoint with the client's authentication (client_secret_basic if the
    provider offers it, else client_secret_post; a public client sends only its id)."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    form = dict(form)
    if cfg.get("client_secret"):
        offered = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in offered:
            pair = urllib.parse.quote(cfg["client_id"], safe="") + ":" + urllib.parse.quote(cfg["client_secret"], safe="")
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        else:
            form.update(client_id=cfg["client_id"], client_secret=cfg["client_secret"])
    else:
        form["client_id"] = cfg["client_id"]
    return _request(doc["token_endpoint"], urllib.parse.urlencode(form).encode("ascii"), headers)


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise OIDCError("The provider sent an ID token that can't be read.")


def finish(cfg, state, code):
    """Exchange the code for the user's claims after the provider sent the browser back.
    Returns (claims including userinfo, the next URL stored at start)."""
    with _LOCK:
        pending = _PENDING.pop(state, None)
    if not pending or time.time() - pending["created"] > PENDING_TTL:
        raise OIDCError("This sign-in expired or was already used. Start it again.")
    if pending["issuer"] != cfg["issuer"].rstrip("/") or pending["client_id"] != cfg["client_id"]:
        raise OIDCError("The single sign-on settings changed during the sign-in. Start it again.")
    doc = discover(cfg["issuer"])
    status, tokens = _token_request(doc, cfg, {"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"]})
    if status != 200 or not tokens.get("id_token"):
        err = tokens.get("error") or f"HTTP {status}"
        hint = " Check the client ID and secret." if err == "invalid_client" else ""
        raise OIDCError(f"The provider refused the sign-in ({err}).{hint}")
    claims = _claims(tokens["id_token"])
    now = time.time()
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if claims.get("iss", "").rstrip("/") != doc["issuer"].rstrip("/"):
        raise OIDCError("The ID token comes from another issuer.")
    if cfg["client_id"] not in auds or (len(auds) > 1 and claims.get("azp") not in (None, cfg["client_id"])):
        raise OIDCError("The ID token is meant for another client.")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - CLOCK_SKEW:
        raise OIDCError("The ID token has expired. Check that this machine's clock is right.")
    if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + CLOCK_SKEW:
        raise OIDCError("The ID token was issued in the future. Check that this machine's clock is right.")
    if not secrets.compare_digest(str(claims.get("nonce", "")), pending["nonce"]):
        raise OIDCError("The ID token doesn't belong to this sign-in.")
    if not claims.get("sub"):
        raise OIDCError("The ID token names no user.")
    if doc.get("userinfo_endpoint") and tokens.get("access_token"):
        try:
            st, info = _request(doc["userinfo_endpoint"], headers={"Authorization": "Bearer " + tokens["access_token"]})
        except OIDCError:
            st, info = 0, {}
        if st == 200 and isinstance(info, dict) and info.get("sub") == claims["sub"]:
            claims = {**info, **claims}         # the ID token's claims win
    return claims, pending["next"]


def allowed(claims, users, groups):
    """Whether the signed-in user may use the web UI: users lists subjects, user names or e-mail
    addresses (or "*" = everyone the provider lets sign in), groups the groups whose members may."""
    names = {str(claims.get(k, "")).casefold() for k in ("sub", "preferred_username", "email") if claims.get(k)}
    if "*" in users or names & {u.casefold() for u in users}:
        return True
    have = claims.get("groups") or []
    if isinstance(have, str):
        have = have.split()
    return bool({str(g).casefold() for g in have} & {g.casefold() for g in groups})


def display_name(claims):
    return str(claims.get("preferred_username") or claims.get("email") or claims.get("name") or claims["sub"])


def test(cfg, redirect_uri):
    """(ok, message) of a connection test: discovery, then the client's credentials at the token
    endpoint (an unknown code must be refused with invalid_grant, not invalid_client)."""
    try:
        doc = discover(cfg["issuer"], fresh=True)
    except OIDCError as e:
        return False, str(e)
    if not cfg["client_id"]:
        return False, "Found the provider. Enter the client ID."
    try:
        status, body = _token_request(doc, cfg, {"grant_type": "authorization_code", "code": "handlebreaks-connection-test",
                                                 "redirect_uri": redirect_uri, "code_verifier": secrets.token_urlsafe(48)})
    except OIDCError as e:
        return False, str(e)
    err = body.get("error") if isinstance(body, dict) else None
    if err == "invalid_client" or status == 401:
        return False, ("The provider rejected the client ID or secret (invalid_client)."
                       + ("" if cfg.get("client_secret") else " This client may need a secret."))
    if err in ("invalid_grant", "invalid_request") or status == 400:
        scopes = doc.get("scopes_supported")
        missing = [s for s in cfg["scopes"].split() if scopes and s not in scopes]
        note = f" The provider doesn't list the scopes {', '.join(missing)}." if missing else ""
        return True, (f"Connected to {doc['issuer']}; the client is accepted. Sign in once to finish the check "
                      f"(the redirect URL registered at the provider must be exactly {redirect_uri}).{note}")
    return False, f"Unexpected answer from the token endpoint (HTTP {status}{', ' + err if err else ''})."
