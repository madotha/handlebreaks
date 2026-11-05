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

---

## 5. Endpoints, phase 1 (read)

Common objects, used in several responses:

**File** (`library:read` and wherever a file appears)
```json
{"key": "share2/Northwind Station (2018)/Season 02/Northwind.Station.S02E01.1080p.WEB.h264-NTB.mkv",
 "share": {"number": 2, "name": "Shows"},
 "folder": "Northwind Station (2018)/Season 02", "name": "Northwind.Station.S02E01.1080p.WEB.h264-NTB.mkv",
 "size": 2147483648, "added": "2026-10-30T21:14:00+01:00",
 "video": {"codec": "h264", "codec_name": "H.264 (AVC)", "optimized": false, "width": 1920, "height": 1080,
           "resolution": "1080p", "duration": 2712.4, "bitrate": 6334000},
 "probe": {"probed": "2026-10-31T02:10:00+01:00", "error": null},
 "status": "offer", "status_label": "Offered", "last_result": {"status": "FAIL", "at": "…"}}
```
- `status` is one of `offer`, `queued`, `waiting` (encoded, awaiting review), `h265` (already optimized), `bad` (filtered release type), `skipped`, `baseline`; `optimized` is true for HEVC, AV1 and VVC; `video` fields are `null` until the file was probed; `last_result` is `null` without one.

**Preset reference**: `{"id": "1080p-x265", "name": "1080p x265 (Ger/Eng) RF20"}`; `null` means the share's (or default) preset.

### 5.1 `GET /api/v1/`: any valid token
```json
{"api_version": "1.0", "handlebreaks_version": "4.2.0",
 "token": {"id": "k3m9q2xa", "name": "Homepage", "scopes": ["status:read", "…"], "expires": "…"},
 "docs": "/api/v1/openapi.json"}
```

### 5.2 `GET /api/v1/summary`: `status:read`
One flat object for dashboards (Homepage's `customapi` widget maps fields by dotted path). Fields of scopes the token lacks are `null` (so one dashboard token with `status:read` alone still works; with `queue:read`, `review:read`, `stats:read` the numbers fill in).
```json
{"encoding": {"active": true, "file": "Northwind.Station.S02E03.mkv", "percent": 41.2, "fps": 38.5,
              "eta_seconds": 1260, "position": 3, "of": 12},
 "worker": {"state": "encoding", "paused": false},
 "queue": {"waiting": 9, "next_run": 4},
 "review": {"waiting": 5, "saved_bytes": 9126805504},
 "history": {"done_24h": 7, "failed_24h": 1},
 "library": {"files": 70312, "optimized_percent": 38.4, "to_encode": 41022},
 "saved": {"bytes": 1849267937280},
 "offline": [], "scanning": false}
```
- `worker.state`: `encoding`, `idle`, `paused` (after Stop all, batches waiting), `waiting` (share offline, HandBrakeCLI missing or not readable: see `/status`), `off` (Docker with `WORKER_ENABLED=0` and no native worker running).

### 5.3 `GET /api/v1/status`: `status:read`
```json
{"current": {"file": {…File…}, "position": 3, "of": 12, "percent": 41.2, "fps": 38.5, "eta_seconds": 1260,
             "started": "2026-11-05T14:02:00+01:00", "preset": {…}},
 "worker": {"state": "encoding", "paused": false, "stoppable": true,
            "waiting_for": null},
 "offline": [{"share": {"number": 1, "name": "Movies"}, "since": "…"}],
 "scan": {"running": false, "label": null, "started": null, "last_finished": "…"},
 "replaces": {"waiting": 0, "moving": 0, "checking": 0}}
```
- `current` is `null` while nothing encodes. `waiting_for` = `{"kind": "offline"|"denied"|"handbrake", "share": {…}|null, "error": "…"}` while the worker waits (the web UI's banner).

### 5.4 `GET /api/v1/queue`: `queue:read`
Parameters: `state` (`waiting`, `encoding`, `removed`; default all three), `q` (words in the key), `limit`, `cursor`, `sort` (`order` = default, `file`, `codec`, `resolution`, `bitrate`, `preset`, `state`), `dir`.
```json
{"items": [{"position": 3, "state": "encoding", "run": true, "file": {…File…}, "preset": {…}|null},
           {"position": 4, "state": "waiting", "run": true, "file": {…}, "preset": null}],
 "total": 12, "next": null}
```
- `position` = the worker's order; `run` = part of the current worker run (false: starts with the next run).

### 5.5 `GET /api/v1/shares`: `library:read`
```json
{"items": [{"number": 2, "name": "Shows", "reachable": true, "preset": {…}|null, "files": 31877,
            "last_scan": "…", "optimized_percent": 41.0}], "total": 2, "next": null}
```
Paths of the shares are **not** included (they reveal the host's layout and aren't needed by clients).

### 5.6 `GET /api/v1/folders?path=`: `library:read`
One level of the tree, like the Files page. `path` = `share<N>` or `share<N>/<folder>`; empty = the shares.
Parameters: the file filters of 5.7 (counts then follow them), `limit`, `cursor`.
```json
{"path": "share2/Northwind Station (2018)",
 "folders": [{"path": "share2/Northwind Station (2018)/Season 02", "name": "Season 02", "files": 10,
              "size": 21474836480, "optimized": 2, "unknown": 0, "other": 8,
              "states": {"offered": 6, "queued": 1, "to_review": 1, "failed": 0}}],
 "files": {"items": [{…File…}], "total": 0, "next": null}}
```
- A `path` that isn't inside a share gets 404 (no `..`, no absolute paths; it is only ever matched against keys in the database, never opened on disk).

### 5.7 `GET /api/v1/files`: `library:read`
Search across the library. Parameters (all optional, combined with AND; repeatable ones as `codec=h264&codec=mpeg4`):
- `path`: limit to a share or folder (recursive)
- `q`: words in the key (case-insensitive, every word must match)
- `codec`: codec names, `unknown`, `group:good`, `group:other`
- `status`: `offer`, `queued`, `waiting`, `h265`, `bad`, `skipped`, `baseline`
- `added`: a range spec as on the Files page (`24h`, `7d`, `3m-1m`, `-1y`) or `added_from` / `added_to` (dates, `YYYY-MM-DD`)
- `min_size`, `max_size`, `min_bitrate`, `max_bitrate`, `resolution` (`4K`, `1440p`, `1080p`, `720p`, `576p`, `480p`, `SD`)
- `sort` (`name`, `codec`, `resolution`, `bitrate`, `duration`, `size`, `added`, `status`), `dir`, `limit`, `cursor`
- `facets=1` adds per-choice counts (`{"codec": {"h264": 812, …}, "status": {…}, "added": {…}}`), each counted with the other filters, like the Files page's filter panel.

Answer: a list of File objects.

### 5.8 `GET /api/v1/file?key=`: `library:read`
One File, plus `queue` (`{"position", "state", "preset"}` or `null`), `encode` (`{"name", "size", "bitrate", "finished", "preset"}` while an encode waits in the output folder, else `null`) and `history` (the file's last 20 History entries).

### 5.9 `GET /api/v1/review`: `review:read`
Awaiting review: files with a finished encode in the output folder.
Parameters: `q`, `sort` (`file`, `preset`, `codec`, `resolution`, `bitrate`, `size`, `saved`, `finished`), `dir`, `limit`, `cursor`.
```json
{"items": [{"file": {…File…}, "encode": {"name": "Northwind.Station.S02E01.1080p.WEB.x265-NTB.rf20.mkv",
            "size": 734003200, "bitrate": 2165000, "finished": "…", "preset": {…}},
            "saved_bytes": 1413480448, "saved_percent": 65.8,
            "replace": null}],
 "total": 5, "next": null, "saved_bytes": 9126805504}
```
- `replace` = `{"id", "state": "waiting"|"moving"|"checking"}` while a replace job for the file is open.

### 5.10 `GET /api/v1/history`: `review:read`
Parameters: `status` (`OK`, `FAIL`, `STOPPED`, `MISSING`, `EXISTS`, `REPLACED`), `q`, `since` (time), `include_dismissed=1`, `sort` (`at`, `file`, `status`), `dir`, `limit`, `cursor`.
```json
{"items": [{"at": "…", "status": "FAIL", "file": {…File…}, "can_retry": true, "log": "f-20261105-140211"},
           {"at": "…", "status": "REPLACED", "file": {…}, "replaced_by": {…File…}, "plex": "ok"|"pending"|"scanning"|"failed"|"skipped"|null}],
 "total": 214, "next": "…"}
```
- `log`: id of the HandBrake log for FAIL entries (see 5.15), `null` if there is none.
- History is limited by `HISTORY_KEEP_DAYS` like the web UI, unless `since` asks for older entries.

### 5.11 `GET /api/v1/replace/{id}`: `review:read`
The state of one replace job: `{"id", "file", "new_name", "keep_original", "state": "waiting"|"moving"|"checking"|"done"|"failed"|"cancelled", "message", "plex", "finished"}`. (Starting and cancelling: phase 3.)

### 5.12 `GET /api/v1/presets` and `GET /api/v1/presets/{id}`: `presets:read`
List: `{"items": [{"id", "name", "default": true|false, "used_by_shares": [2], "summary": {"encoder": "x265_10bit", "quality": "rf20", "container": "mkv", "resolution_limit": "1920x1080", "audio": ["ger: AAC stereo", "eng: passthru"]}, "compatible": true, "notes": []}]}`.
One preset: the same plus `"handbrake": {…}`, the complete HandBrake export (`PresetList[0]`) as stored, so a client can see every setting. `compatible`/`notes` come from the same check as the preset editor (settings the encoding HandBrakeCLI ignores).

### 5.13 `GET /api/v1/stats`: `stats:read`
```json
{"library": {"files": 70312, "size": 211106232532992, "optimized_files": 27020, "optimized_size": 81064793292800,
             "codecs": {"h264": 39871, "hevc": 26840, "unknown": 98, "…": 0},
             "statuses": {"offer": 41022, "h265": 26840, "…": 0},
             "resolutions": {"4K": 1203, "1080p": 41552, "…": 0}},
 "encodes": {"total": 2140, "failed": 37, "per_day": [{"date": "2026-11-04", "ok": 14, "failed": 1}]},
 "saved": {"bytes": 1849267937280, "replaced_files": 1980},
 "kept_originals": {"files": 12, "size": 24696061952}}
```
`per_day` covers the last 30 days. Every number is the same as on the Stats page.

### 5.14 `GET /api/v1/events?after=<id>`: `events:read`
The notification feed (the same events as the web UI's popups, kept 7 days).
```json
{"items": [{"id": 9182, "at": "…", "kind": "encode_fail", "group": "encodes", "level": "error",
            "title": "Encode failed", "file": {"key": "…", "name": "…"}, "detail": "HandBrakeCLI exit 3"}],
 "last_id": 9182}
```
- Without `after`: only `last_id` (start of polling). `kind` is one of `encode_ok`, `encode_fail`, `run_done`, `run_interrupted`, `replace_done`, `replace_failed`, `plex_failed`, `scan_new`, `scan_done`, `scan_failed`, `offline`, `online`, `purge`, and from phase 2 `api_write` and `api_token_expiring`. Unlike the popups, the feed isn't filtered by `NOTIFY_EVENTS`.

### 5.15 Logs: `logs:read`
- `GET /api/v1/logs/failures` → `{"items": [{"id": "f-20261105-140211", "at": "…", "file": {"key", "name"}|null, "size": 48213}]}`, newest first, the HandBrake logs of failed encodes (`logs/fail-*.log`).
- `GET /api/v1/logs/failures/{id}` → `text/plain` (UTF-8, at most the last 2 MB). Only files matching `fail-<YYYYMMDD-HHMMSS>.log` inside `logs/` are reachable; the id is checked against that pattern, never used as a path.
- Change in `encode.sh` (installed by rename, as always for the running worker): the first line of each fail log becomes `# Handlebreaks: <key>`, so a log names its file. Older logs are matched to a FAIL result within 2 minutes of their time; `file` is `null` if none fits.

### 5.16 `GET /api/v1/system`: `system:read`
```json
{"handlebreaks_version": "4.2.0", "api_version": "1.0",
 "mode": "docker-ui-native-worker",
 "handbrake": {"here": "1.11.2", "worker": "1.11.2", "worker_platform": "macOS"},
 "ffprobe": true, "python": "3.13.5",
 "output_folder": {"reachable": true, "free_bytes": 1294018101248, "total_bytes": 7999995002880},
 "shares": [{"number": 2, "name": "Shows", "reachable": true}],
 "uptime_seconds": 61234}
```
- `mode`: `native`, `docker`, `docker-ui-native-worker`. No paths, no settings values, no environment variables.

---

## 6. Endpoints, phase 2 (write) and the activity page

All write endpoints: `POST` with a JSON body, `Idempotency-Key` supported (1.6), each successful call raises an `api_write` event naming the token, the action and the number of files ("API (Homepage): 3 files queued"), in the notification group `api` (new, on by default).

Answers of bulk operations always list what happened per file, so nothing fails silently:
```json
{"done": ["share2/…S02E01.mkv"], "skipped": [{"key": "share2/…S02E02.mkv", "reason": "already_queued"}],
 "rejected": [{"key": "share9/x.mkv", "reason": "unknown_key"}]}
```
A request whose every item is rejected answers **422**; partly successful requests answer **200** with the lists.

### 6.1 Queue: `queue:write`
- `POST /api/v1/queue` `{"keys": [...], "folders": [...], "filters": {…as 5.7…}, "preset": "id"|null}`. Folders are expanded with the filters, like ticking a folder on the Files page (only files that could be queued: status `offer`, or `skipped`/`bad` when named explicitly by key). `preset` overrides the share's preset for these files; an unknown preset is 422. Skipped reasons: `already_queued`, `encoding`, `already_optimized`, `awaiting_review`, `replace_pending`. Written as one batch through `write_batch()`, the function the web UI and the command line use.
- `POST /api/v1/queue/remove` `{"keys": [...]}` or `{"all": true}`: waiting entries only (the current encode is stopped with 6.2).
- `POST /api/v1/queue/move` `{"keys": [...], "op": "top"|"bottom"|"up"|"down"}` or `{"keys": [...], "before": "<key>"}`. Only waiting entries; answers the new order (positions).
- `POST /api/v1/queue/retry` `{"keys": [...]}` or `{"failed": true}`: queue files again whose last result is FAIL or STOPPED (with the preset of their last batch, like **Queue again**).

### 6.2 Encoding control: `queue:write`
- `POST /api/v1/encoding/stop` `{"mode": "skip"|"all", "key": "<the current key>"}`: `skip` stops the current file and continues with the next; `all` stops and pauses (the rest goes back to the queue). `key` must match the file being encoded (409 `not_current` otherwise), so a delayed request never stops the wrong file. 409 `not_stoppable` with a worker older than the stop support.
- `POST /api/v1/encoding/resume` `{}`: ends the pause after **Stop all**.

### 6.3 Scans: `library:scan`
- `POST /api/v1/scans` `{"share": 2}` or `{"path": "share2/Northwind Station (2018)"}` or `{}` (everything); optional `"force": true` (read every file with ffprobe again). Answers **202** `{"scan": {"running": true, "label": "…", "started": "…"}}`; progress via `GET /api/v1/status` (`scan`). 409 `scan_running` while one runs; 409 `share_offline` for an unreachable share.

### 6.4 Review: `review:write`
- `POST /api/v1/encodes/delete` `{"keys": [...], "requeue": false, "preset": null}`: deletes the finished encodes in the output folder; with `requeue` the files are queued again (with `preset`, `null` = the share's). Skipped: `replace_pending`, `queued`, `no_encode`.
- `POST /api/v1/history/dismiss` `{"keys": [...]}` or `{"older_than_days": 30}` or `{"all": true, "status": "FAIL"|null}`: hides History entries (nothing on disk changes), like the Status page.

### 6.5 API activity page (Settings → System → API → Activity)
- Shows the last 500 API requests (kept 7 days): time, token (name and id, or "unknown token"), address, method, path (without query values for keys, which are shortened), status code, and for writes the action and number of files. Filters: token, failed only, writes only.
- Failed authentication is listed with the address (and the token id if one was sent), so a misconfigured dashboard or someone guessing shows up.
- Stored in a new table `api_log` in `encoding.db` (additive, `CREATE TABLE IF NOT EXISTS`; older versions ignore it), pruned hourly with the events.
- The token list links each token to its activity.

---

## 7. Endpoints, phase 3 (opt-in, library-changing)

### 7.1 Replace: `replace:write`
- `POST /api/v1/replace` `{"items": [{"key": "…", "new_name": "…"|null, "keep_original": true}]}`: like **Replace…** in the web UI. `new_name: null` = the name Handlebreaks suggests (`suggest_name()`); `keep_original` defaults to the `KEEP_ORIGINALS` setting. Validated completely before any job is created (unknown key, no finished encode, replace pending, invalid or duplicate name, name exists): if anything is invalid, nothing is started (422 with `details.items`). Answers **202** with the job ids; the worker carries them out (`replace/` job files, as today), the web UI tells Plex.
- `GET /api/v1/replace/progress`: the progress bar's data (`waiting`, `moving`, `checking`, `done`, `failed`, `cancelled` with the jobs), scope `review:read`.
- `DELETE /api/v1/replace/{id}`: cancels a job the worker hasn't started; 409 `already_started` otherwise.
- `GET /api/v1/replace/suggest?key=`: the suggested name (scope `review:read`).

### 7.2 Kept originals: `originals:read` / `originals:write`
- `GET /api/v1/originals`: `{"items": [{"path": "<path inside .replaced/>", "size", "kept_since", "replaced_by": {…File…}|null, "warning": null|"missing"|"offline"|"unknown"|"plex", "saved_bytes"}]}`.
- `POST /api/v1/originals/delete` `{"paths": [...], "force": false}` or `{"older_than_days": 30}`: deletes kept originals, held back like in the web UI when their replacement is missing or offline (unless `force`) or Plex hasn't confirmed it (always held back). Answers done/skipped lists with `freed_bytes`.

---

## 8. Implementation plan

### 8.1 Structure
- **`api.py`**: Flask blueprint at `/api/v1`, registered by `webui.py`. Its `before_request` does: API enabled → rate limit for the address (failed auth) → token lookup → expiry/revoked → allowed networks → rate limit for the token → scope of the route; the result is in `g.token`. `webui.before()` skips the session login and the CSRF check for the blueprint's endpoints (they never use cookies; the blueprint also rejects requests that send none of the above but carry the session cookie, just by ignoring it).
- **`apitokens.py`**: `api-tokens.json` (create, list, revoke, verify, last use), the same atomic 0600 writing as `auth.py`.
- **Route registry**: each API route is declared with `@api_route("GET", "/queue", scope="queue:read", params=…, response=…, summary=…)`. From it come the scope check, the OpenAPI 3.1 document (`GET /api/v1/openapi.json`, needs a token), the reference page in the web UI and the wiki tables; the image workflow fails if a route has no scope, no summary or no response schema (`python3 webui.py --check-api`, like `--check-settings`).
- **Shared service functions** (the main refactor, phase 1 and 2): the logic in today's web UI routes moves into functions that take plain values and return plain results, used by both the web UI routes and the API:
  - phase 1: `status_snapshot()`, `queue_list()`, `library_level()`/`file_search()` (wrapping `tree_level()` and `filter_sql()`), `review_list()`, `history_list()`, `stats_data()` (split out of the Stats page's chart building), `system_info()` (from `about_info()` without paths)
  - phase 2: `queue_add()`, `queue_remove()`, `queue_move()`, `queue_retry()`, `stop_encoding()`, `start_scan()`, `delete_encodes()`, `dismiss_history()`
  - phase 3: `replace_start()` (validation of `/replace/bulk`), `kept_delete()`
  The web UI's pages must behave exactly as before; each moved route is checked in Chromium and WebKit.
- **Settings → System → API** (new settings page): `API_ENABLED`, `API_TRUSTED_PROXIES`; token list (name, `hb_<id>_…`, scopes, created, expires, last used and address, opt-in marker), **New token** dialog (name, bundles, single scopes with descriptions, opt-in warning + confirmation, expiry, allowed networks, password/two-factor check), token shown once with copy button, edit name/networks, revoke, revoke all; **Reference** (rendered from the registry: every endpoint with scope, parameters, example answer) and, from phase 2, **Activity**.
- New settings keys: `API_ENABLED`, `API_TRUSTED_PROXIES` (config.example.env, settings registry, `--check-settings`).
- New files in the data folder: `api-tokens.json` (and its temp file) in `.gitignore`, `.dockerignore`, Files-and-Data.

### 8.2 Tests (per phase, in a scratch data folder with the demo data)
- Authentication: no header, wrong scheme, unknown id, wrong secret, expired, revoked, address not allowed, trusted proxy header handling, session cookie alone (must be 401), throttling after failed attempts.
- A **scope matrix**: every route called with a token lacking its scope (403 `missing_scope`) and with exactly that scope (2xx); generated from the registry so new routes are covered automatically.
- Validation: unknown and malicious keys (`..`, absolute paths, other shares), oversized bodies, too many keys, wrong content type, idempotent replays.
- No secrets in any response: every endpoint's answer searched for the Plex token, the OIDC secret, password hashes and the session key set in the test config.
- Rate limit headers and 429.
- The web UI unchanged after the refactor (Files, Status tabs, Stats, Settings), Chromium and WebKit.
- Homepage: a real Homepage container with the `customapi` example of the docs, showing the summary.
- Image workflow: `--check-api`; a token created with `--create-token` in the smoke container answers `GET /api/v1/status` and is refused on `POST /api/v1/queue`.

### 8.3 Documentation
- Wiki page **API**: overview, security concept (from sections 2 and 4), scopes with bundles, every endpoint (generated tables plus `curl` examples), errors, rate limits, idempotency, versioning, Homepage example, troubleshooting (401/403/address).
- Wiki: Security (API section), Configuration (`API_*`), Files-and-Data (`api-tokens.json`), Command-Line (token commands), Web-UI (Settings → API), Home and sidebar (new page).
- README: Features (API), FAQ ("Can I show Handlebreaks on my dashboard?").
- CLAUDE.md: architecture of the API, the registry and the shared service functions, the rule that every API route needs a scope, a summary and a schema.
- CHANGELOG per phase.

### 8.4 Homepage example (wiki)
```yaml
- Handlebreaks:
    icon: https://<host>/static/icons/icon-192.png
    href: https://<host>/status
    description: H.265 encoding
    widget:
      type: customapi
      url: http://<host>:7979/api/v1/summary
      refreshInterval: 15000
      headers:
        Authorization: Bearer hb_xxxxxxxx_…
      mappings:
        - field: encoding.percent
          label: Encoding
          format: percent
        - field: queue.waiting
          label: Queued
        - field: review.waiting
          label: To review
        - field: saved.bytes
          label: Saved
          format: bytes
```
With the dashboard token restricted to Homepage's address (2.4).

### 8.5 Effort (rough)
- Phase 1: the largest (tokens, settings page, registry/OpenAPI, refactor of the read logic, 16 read endpoints, docs). Several sessions.
- Phase 2: medium (write refactor, idempotency, activity page).
- Phase 3: small (the validation already exists in the web UI).
