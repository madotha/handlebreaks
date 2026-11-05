# Handlebreaks API v1: specification (draft)

Status: **draft for review**, not implemented. Written against Handlebreaks 4.1.1.
Once approved, the endpoint parts of this document become the OpenAPI 3.1 description
(`/api/v1/openapi.json`) and the wiki page **API**; this file is then removed.

Phases:

| Phase | Version | Content |
|---|---|---|
| 1 | 4.2.0 | Tokens (web UI, command line), allowed networks per token, every read endpoint, `/summary` for dashboards, `system:read`, `logs:read`, OpenAPI, reference page, wiki |
| 2 | 4.3.0 | Write endpoints: queue, scans, stop/resume, delete encodes, dismiss history; idempotency; API activity page |
| 3 | 4.4.0 | Library-changing scopes (opt-in): `replace:write`, `originals:write` |

---

## 1. Principles and conventions

### 1.1 Base URL and versioning
- Every endpoint lives under `/api/v1/`. Behind a reverse proxy with a subpath, the prefix follows it (`/handlebreaks/api/v1/`).
- **Within v1, only additive changes**: new endpoints, new optional parameters, new response fields, new scopes, new error codes. Clients must ignore fields they don't know.
- **Breaking changes** (removing or renaming a field, changing its type or meaning, a parameter becoming required) only come with `/api/v2/`. v1 then stays available for at least one MAJOR release of Handlebreaks and answers with the header `Deprecation: true` plus `Sunset: <date>`; the changelog announces it.
- `GET /api/v1/` reports `api_version` (`"1.<minor>"`, raised with every addition) and `handlebreaks_version`.

### 1.2 Format
- Requests and responses are JSON (`application/json; charset=utf-8`). Write requests must send `Content-Type: application/json`; anything else gets **415**. No form posts.
- Field names are `snake_case`. Missing values are `null`, never left out (a client can rely on every documented field being there).
- **Times**: ISO 8601 with the server's UTC offset, e.g. `"2026-11-05T14:20:00+01:00"`. Stored times only have minutes; seconds are then `00`.
- **Sizes** in bytes, **bitrates** in bit/s, **durations** in seconds (numbers, not strings); percentages as numbers 0–100.
- **File keys** identify every file, as everywhere in Handlebreaks: `share<N>/<path in the share>`, e.g. `share2/Northwind Station (2018)/Season 02/Northwind.Station.S02E01.mkv`. Because they contain slashes, keys travel in the query string (`?key=…`, URL-encoded) or in the request body, never in the path.
- Responses are gzip-compressed when the client sends `Accept-Encoding: gzip` (as the web UI already does).

### 1.3 Lists and paging
- Lists answer `{"items": [...], "total": <n>, "next": <cursor or null>}`.
- `limit` (default 100, maximum 500) and `cursor` (opaque string from `next`). A cursor is valid for 10 minutes; an expired or foreign cursor gets **400** `invalid_cursor`.
- Sorting where offered: `sort=<field>` and `dir=asc|desc`, the same fields the web UI sorts by.

### 1.4 Errors
Every error has the same shape and a stable machine-readable `code`:

```json
{"error": {"code": "missing_scope", "message": "This token lacks the scope queue:write.", "details": {"scope": "queue:write"}}}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request`, `invalid_cursor`, `invalid_json` | Malformed parameters or body |
| 401 | `unauthenticated` | No token, unknown, expired or revoked token (always the same message: the response never tells which) |
| 403 | `missing_scope` | The token lacks the scope; `details.scope` names it |
| 403 | `address_not_allowed` | The token is restricted to other network addresses |
| 404 | `not_found` | Unknown endpoint, key, preset, job, or the API is turned off (`API_ENABLED=0`) |
| 405 | `method_not_allowed` | |
| 409 | `conflict` | The state doesn't allow it (file already queued, replace already running, scan running); `details.reason` |
| 413 | `too_large` | Body over 1 MB, or more than 5,000 keys in one request |
| 415 | `unsupported_media_type` | Write request without JSON |
| 422 | `validation_failed` | Well-formed but invalid input; `details.fields` maps field → problem, `details.items` lists the rejected keys with reasons |
| 429 | `rate_limited` | Too many requests; header `Retry-After` (seconds) |
| 500 | `internal_error` | Unexpected; the message never contains paths, secrets or stack traces (those go to the log with a request id) |
| 503 | `unavailable` | E.g. the database is locked by a running migration; `Retry-After` |

Every response carries `X-Request-Id` (also written to the log next to any error), so a problem report can name it.

### 1.5 Rate limits
- Per token, sliding window: **120 read requests per minute**, **20 write requests per minute**.
- Every response carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` (IETF draft headers).
- Failed authentication (401) is counted per client address, like the sign-in page: after 10 failures within 15 minutes each further attempt waits longer (up to 15 minutes). A slowed-down address gets 429 even with a valid token, so guessing can't be combined with a working one.
- The limits are in memory: a restart resets them.

### 1.6 Idempotency (write requests, phase 2)
- Write requests may send `Idempotency-Key: <client-chosen string, max 100 characters>`. The first response for a key is stored for 24 hours per token; a retry with the same key and the same body gets that stored response (with `Idempotent-Replayed: true`) instead of running again. The same key with a different body gets **422** `idempotency_key_reused`.
- The write endpoints are also safe to repeat without a key: queueing an already queued file answers it as `skipped` (`reason: already_queued`), deleting an already deleted encode as `skipped` (`reason: not_found`); nothing happens twice.

### 1.7 Caching
- Read responses carry `Cache-Control: no-store` (they may hold library data) and an `ETag`. A client may send `If-None-Match` and gets **304** when nothing changed: cheap polling for dashboards.

---

## 2. Authentication and tokens

### 2.1 How a request authenticates
- **Only** `Authorization: Bearer <token>`. No query-string tokens (they end up in logs and browser history), no cookies: the web UI's session is **not** accepted on `/api/`, so cross-site requests (CSRF) can't reach it, and the API needs no CSRF token.
- A request without valid authentication never gets data: **401** with `WWW-Authenticate: Bearer realm="Handlebreaks"`. This includes `GET /api/v1/openapi.json`.
- The web UI's own pages keep using their session; they don't call the API.

### 2.2 Token format and storage
- Format: `hb_<id>_<secret>`; `id` = 8 characters (lower-case base32, public, used for lookup and shown in the UI), `secret` = 43 characters (256 random bits, base64url). Example: `hb_k3m9q2xa_Jw9…`. The `hb_` prefix lets secret scanners (GitHub, gitleaks) recognize leaked tokens.
- Stored in `api-tokens.json` in the data folder (file mode 0600, written via temp file + rename, gitignored and excluded from the Docker build context) as:

```json
{"tokens": [{"id": "k3m9q2xa", "name": "Homepage", "hash": "<sha256 of the whole token, hex>",
  "scopes": ["status:read", "queue:read", "review:read", "stats:read", "system:read"],
  "allowed": ["192.168.1.20/32"], "created": "2026-11-05T15:00:00+01:00", "created_by": "admin",
  "expires": "2027-02-03T15:00:00+01:00", "last_used": "…", "last_address": "192.168.1.20", "revoked": null}]}
```

- Only the SHA-256 hash is kept: the secret has 256 bits of entropy, so a slow password hash isn't needed, and lookups stay fast. The check finds the entry by `id`, then compares hashes with `hmac.compare_digest`.
- `last_used` and `last_address` are written at most once a minute per token (no disk write per request).
- Revoked tokens stay in the file with `revoked` set (for the activity page) and are removed 30 days later.

### 2.3 Lifecycle
- **Create**: Settings → System → API → **New token** (web UI) or `webui.py --create-token` (command line on the machine). The web UI asks for the password again (plus the two-factor code if it is on); a signed-in single sign-on user instead needs a sign-on within the last 10 minutes (otherwise the page sends them through the provider again). A stolen browser session alone can't create tokens.
- **Fields**: name (required, unique, 1–64 characters), scopes (at least one), expiry (30, 90 = default, 365 days or never), allowed networks (optional).
- **Shown once**: the complete token appears only in the response to its creation, with a copy button and the hint that it can't be shown again. Afterwards the UI shows name, `hb_<id>_…`, scopes, created, expires, last used and from where.
- **Edit**: name and allowed networks can change; scopes and expiry can't (create a new token instead, so a token never silently gains rights).
- **Revoke** one token, or **Revoke all**. Takes effect immediately.
- **Expiry**: an expired token answers 401 like an unknown one. The settings page marks tokens expiring within 14 days, and an event (`api_token_expiring`, group `system`) is raised once 7 days before.
- **Limits**: at most 50 active tokens.
- Changing the account password or "Sign out everywhere else" does **not** revoke tokens (they are separate credentials); the page says so, and **Revoke all** sits next to them.

### 2.4 Allowed networks per token
- Optional list of addresses or CIDR ranges (IPv4 and IPv6), e.g. `192.168.1.20/32`, `172.18.0.0/16`. Empty = any address.
- Checked against the TCP peer address (`REMOTE_ADDR`). `X-Forwarded-For` is ignored unless `API_TRUSTED_PROXIES` lists the proxy's address; then the rightmost untrusted address of the header counts.
- A token used from another address: **403** `address_not_allowed` (after the hash check, so it doesn't reveal whether the token exists), plus an entry on the activity page.
- Docker note in the docs: a client in another container usually reaches Handlebreaks from the Docker network's gateway or its container address; the activity page shows which address to allow.

### 2.5 Command line (on the machine, like `--set-password`)
```
python3 webui.py --create-token NAME --scopes status:read,queue:read [--expires 90|30|365|never] [--allow 192.168.1.0/24]
python3 webui.py --list-tokens
python3 webui.py --revoke-token ID|--all
```
`--create-token` prints the token once on stdout (nothing else on stdout, so scripts can capture it). Docker: prefix with `docker compose exec encoding`.

### 2.6 Switching the API off
- `API_ENABLED` in `config.env` (Settings → System → API) or as environment variable (wins). Default **1**, but without a token nothing is reachable. `0`: every `/api/` URL answers 404 as if it didn't exist; tokens are kept.

### 2.7 Transport
- Handlebreaks serves plain HTTP. Tokens sent over a network without HTTPS can be read by anyone on the path. The docs and the token page say so; recommended: a reverse proxy with HTTPS, a VPN, or keeping the API on the host/Docker network (Homepage on the same machine).
- CORS: no `Access-Control-Allow-Origin` header, so browsers on other sites can't read API responses. Homepage calls the API from its server and doesn't need CORS.

---

## 3. Scopes

| Scope | Allows | Phase |
|---|---|---|
| `status:read` | Current encode, worker state, paused, shares offline, scan running, `/summary` | 1 |
| `queue:read` | Queue entries with order, preset and state | 1 |
| `library:read` | Shares, folders, files with probe data and status, filter facets | 1 |
| `review:read` | Awaiting review, History, replace jobs' status | 1 |
| `presets:read` | Presets with their settings | 1 |
| `stats:read` | Statistics of the library and of saved space | 1 |
| `events:read` | The notification feed | 1 |
| `system:read` | Versions, HandBrakeCLI of the web UI and the worker, free space of the output folder, how the install runs (no paths of secrets, no settings values) | 1 |
| `logs:read` | HandBrake's log of a failed encode | 1 |
| `queue:write` | Queue files and folders (with preset), remove, reorder, queue failed files again, stop and resume encoding | 2 |
| `library:scan` | Start scans of everything, a share or a path | 2 |
| `review:write` | Delete finished encodes (optionally queue them again), dismiss History entries | 2 |
| `replace:write` | Replace originals with their encodes, cancel waiting replaces | 3, **opt-in** |
| `originals:read` | List kept originals | 3 |
| `originals:write` | Delete kept originals | 3, **opt-in** |

Rules:
- Scopes are exact; a `:write` scope doesn't imply its `:read` scope (a token that queues but can't list the queue is valid).
- No scope gives access to: tokens, the account, login and API settings, any other settings, secrets (Plex token, OIDC secret, password hashes, session key), or arbitrary files.
- **Bundles** in the token form (they only tick boxes; the token stores the single scopes):
  - *Dashboard (read-only)*: `status:read queue:read review:read stats:read system:read`
  - *Read everything*: every `:read` scope except `originals:read`
  - *Automation*: *Read everything* plus `queue:write library:scan review:write`
- **Opt-in scopes** (`replace:write`, `originals:write`) are in no bundle; ticking one shows a warning ("This token can change files in your library / delete kept originals") and needs an extra confirmation checkbox. Tokens holding one are marked on the token list.

---

## 4. Threat model

Assets: the media library (files, Plex's view of it), kept originals, the queue and the worker's time, the account and settings, secrets (Plex token, OIDC secret), and information about the library (titles reveal what someone owns).

| Attacker | What they could try | Countermeasures |
|---|---|---|
| Someone on the network without a token | Read data, change anything | Every endpoint needs a token; 401 without data; failed attempts slowed down per address; API can be switched off |
| A website open in the user's browser (CSRF, DNS rebinding) | Use the user's session against the API | The API ignores cookies; only Bearer tokens; no CORS; JSON-only write requests (a cross-site form can't send `application/json` without a CORS preflight, which fails) |
| A leaked token (log, screenshot, dashboard config in a public repo) | Whatever its scopes allow, from anywhere | Least privilege by scopes and bundles; destructive scopes opt-in; allowed networks per token; expiry by default (90 days); revoke in one click; last use and address visible; `hb_` prefix for secret scanners; tokens never in URLs |
| A compromised dashboard host (e.g. Homepage container) | Use the token it holds | A dashboard token is read-only and limited to that host's address; no scope reaches settings or secrets |
| Someone with a valid write token | Flood the queue, delete encodes, replace files | Scopes; rate limits; keys validated against the database (no arbitrary paths reach the worker); every write logged as an event with the token's name; replace and originals deletion opt-in; replace keeps originals by default |
| An attacker who obtains `api-tokens.json` | Recover tokens | Only SHA-256 hashes of 256-bit secrets are stored, which can't be reversed; file mode 0600 |
| A user's browser session is stolen | Create a token for lasting access | Creating a token needs the password (and two-factor) again, or a fresh single sign-on |
| Information leaks in responses | Learn secrets or internals | Responses never contain secrets; errors carry no paths or stack traces; settings aren't exposed; `system:read` reports versions and free space, not configuration |
| Resource exhaustion | Slow down the web UI | Rate limits; `limit` capped at 500; body ≤ 1 MB; ≤ 5,000 keys per request; expensive listings reuse the web UI's caches |

Residual risks (documented): tokens over plain HTTP can be sniffed (use HTTPS/VPN); a token with `replace:write` can change the library within the rules of Replace… (no overwrite, original kept by default); rate limits reset on restart.
