# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Maintaining this file and TODO.md

- Keep this CLAUDE.md in sync with the tool: whenever a change affects commands, architecture, file contracts or configuration, update the relevant section in the same change.
- `TODO.md` tracks all planned work, general and personal. It is gitignored and stays local, so it may be missing in a fresh clone. Sort every new TODO into one of its existing categories (Repository & Infrastructure, Configuration, Features, Bugs & Fixes, Documentation); add a new category only if none fits. Every TODO gets a priority (`**P1**` highest to `**P3**` lowest) right after the checkbox, and open items are sorted by priority within their category. Check items off (`- [x]`) when done.

## Keep the repo generic

The repo is meant to be installed on any machine and may be published. It must stay free of anything specific to one host or person.

- Don't commit host-specific facts, setup notes or TODOs about a particular machine. This covers usernames, home paths, mounted shares, installed versions and the state of a local install. Mention them in chat or put them in the local `TODO.md`.
- Secrets (tokens, passwords, keys, `.env`) and local config or overrides go in files that `.gitignore` covers. When adding a new file or directory of that kind, or one not needed to install the tool elsewhere (runtime output, caches, editor files), add it to `.gitignore` in the same change.
- When code needs a machine-specific value, prefer a gitignored local file plus a committed example over hard-coding it.

## Versioning and changelog

- Semantic Versioning `vX.Y.Z`. The single source of truth is `__version__` in `encoding.py`, shown by `python3 encoding.py version`. It covers the whole tool, including `encode.sh`, `status.sh`, the plist and `preset.json`.
  - MAJOR: incompatible changes, such as the `state.json` schema, the queue, results or `current.txt` file formats, renamed or removed subcommands, or anything that requires reinstalling the launchd agent.
  - MINOR: new backward-compatible features, subcommands or options.
  - PATCH: bug fixes and small adjustments, including changes to filters or presets that don't change the behaviour of the interface.
- Every user-visible change gets an entry under `## [Unreleased]` in `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format: Added, Changed, Deprecated, Removed, Fixed, Security). Pure doc or repo housekeeping doesn't need one.
- To release:
  1. Bump `__version__`.
  2. Rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and add a fresh empty `[Unreleased]` section.
  3. Commit as `Release vX.Y.Z`.
  4. Tag that commit with an annotated tag `vX.Y.Z` (`git tag -a vX.Y.Z -m "vX.Y.Z"`).

  Every release commit must carry its tag. Never bump the version without tagging, and never tag a commit that isn't a release.

## Overview

Semi-automatic H.264 → H.265 (x265) re-encoding of a Plex library with HandBrakeCLI. It runs natively on macOS (launchd) or in Docker. Python 3.9+ plus zsh scripts. `encoding.py` and `encode.sh` use only the standard library and system tools. Only the web UI needs packages (`requirements.txt`: Flask, waitress). ffprobe (ffmpeg) is optional but needed for codec data. There is no build system, linter or test suite; test changes manually in an isolated data folder (`ENCODING_DATA_DIR=<scratch dir>`), and for Docker, with a separate compose project (`docker compose -p <name>`).

**Everything is in English**: code, comments, CLI and log output, the web UI, the Obsidian notes, configs, README and other docs. Don't add German anywhere. The only exceptions are `LEGACY_PREFIXES` and `LEGACY_NOTE_HEADING` in `encoding.py`, which must match data written by version 1.x. The README and is the full from-scratch setup guide for all three setups (native, Docker, Docker + native encoding); update it whenever setup steps, requirements, settings or commands change.

## Commands

```bash
python3 encoding.py scan [--json] [--no-probe]   # scan NAS (+ ffprobe), refresh list, DB and Obsidian note
python3 encoding.py queue all -3 -7 --skip-rest  # queue by number from last scan (also: 1,2,5-8)
python3 encoding.py skip 4 | skipped | unskip 2
python3 encoding.py status
python3 encoding.py version                      # print __version__ (also --version, -V); works without config
python3 encoding.py import-legacy <cache.json> <note.md>   # one-time migration from old encoding_todo.py
zsh encode.sh                                    # run the worker manually (normally launchd or the Docker loop)
zsh status.sh                                    # live terminal progress view
python3 webui.py [--host H] [--port P]           # web UI, default 127.0.0.1:7979 (needs requirements.txt)
docker compose up -d --build                     # Docker: everything in one container
docker compose -f docker-compose.yml -f docker-compose.hybrid.yml up -d   # Docker UI/scan + native Mac encoding
```

Running `scan` or `queue` has real side effects (writes `encoding.db`, the Obsidian note, and `queue/` batches that a worker immediately picks up and starts encoding). Don't run them casually against a real install.

## Architecture

Processes coordinate only through files in the data folder (`DATA_DIR`: the program folder, or `$ENCODING_DATA_DIR`, `/data` in Docker):

- **`encoding.py`** (controller and core library): walks the shares (`shares()` from `SHARE_N` settings), gets size and mtime per file and probes new or changed files with ffprobe (`inventory()`, `FFPROBE_WORKERS` in parallel with the lighter `PROBE_ARGS`, cached by size and mtime in the DB). It classifies each video, stores everything in SQLite and writes `Encoding To-Do.md`. On `queue` it writes `queue/batch-<timestamp>.txt` via a temp file in `DATA_DIR` plus `os.replace`, so a worker never sees a partial file. `scan()` takes a `flock` on `.scan.lock`, so web and CLI scans can't overlap. Replacing originals is a job handoff because the web UI may not write to the library: `POST /replace` validates and calls `encoding.request_replace()` (writes `replace/<id>.json`); `encode.sh` runs `encoding.py replace-jobs` (`run_replace_jobs()`: file moves only, no database) at start and every minute while encoding, writes `replace/done/<id>.json`; the dialog (`_replace_dialog.html`) and its script (`static/replace.js`, `window.ReplaceDialog.attach(root, {say, done})`) are shared by the Files page (re-scans the folder when done) and the Status page (`replace_info()` marks finished rows with their encode or as replaced via `encoding.replaced_keys()`; reloads when done). The page polls `/replace/<id>` for the job's result. `replace_one()` never overwrites another file, keeps or deletes the original, and rolls back on failure. Afterwards it calls `plex.notify_replaced()` (only with `PLEX_URL`/`PLEX_TOKEN`): `plex.Plex` maps the target path to a library by its folder paths, finds the item by walking `/library/sections/<id>/folder` (the folder view merges all folders of a library), and calls `PUT /library/metadata/<ratingKey>/analyze` for a same-name file, otherwise `/library/sections/<id>/refresh?path=<folder>`. The token goes only into the `X-Plex-Token` header; never print or log it. `PLEX_URL` may list several addresses, the first answering one is used. `scan(path=key)` checks one file or folder (the web UI's ↻ buttons, always with `force`): it walks only that path (guarded against leaving the share), replaces only its rows, and doesn't touch `last_scan` or the per-share scan times. `scan(only=[prefixes], force=…)` scans single shares: `save_files()` then replaces only those shares' rows, the offered list and summary are rebuilt from the whole `files` table, and `force` skips the ffprobe cache. The last scan time per share is stored in `meta` as `share_scan:<prefix>`. `load_config()` also migrates a version 2.x `config.env` (`migrate_v2()`, under `.migrate.lock`) and makes sure a preset and `DEFAULT_PRESET` exist. `write_config()` is the only writer of `config.env` (web UI and migration).
- **`presets.py`**: the preset store, `presets/<id>.json` in the data folder, each a HandBrake export file with one preset. The id is a slug fixed at creation; shares and batch lines refer to it, so renaming is safe. It also reads what the installed HandBrakeCLI supports (`capabilities()`: encoders, encoder speed presets, containers, mixdowns, preset format version; `builtin_presets()`; `from_builtin()` via `--preset-export`), cached on disk in `.handbrake-cache.json` (`_disk_cache()`, keyed by the HandBrakeCLI path, size and mtime), and `verify()` makes HandBrakeCLI load a preset before it's saved. Nothing about HandBrake's options is hard-coded except `FALLBACK` for machines without HandBrakeCLI. `apply_form()` maps the editor's common settings onto the preset JSON.
- **`encode.sh`** (worker): started by launchd (`local.plex-encoding.plist`, `QueueDirectories` on `queue/`) or by the loop in `docker-entrypoint.sh`. It moves batches `queue/ → processing/ → done/`, encodes into `<ENCODED>/.inprogress/` and moves the file to `<ENCODED>/` only on success. A `mkdir`-based `.lock` (stale-PID cleanup) keeps it single-instance. It uses `caffeinate` where available, and exits or sleeps if the NAS, HandBrakeCLI or `config.env` is missing, so it gets retried.
- **`webui.py`** (Flask, served by waitress): imports `encoding` as a module and reuses its functions (`load_files`, `current_progress`, `queued_keys`, `recent_results`, `scan`). It reloads `config.env` on every request. Scans run in a background thread (`start_background_scan()`); `auto_scan_loop()`, started in `main()`, checks once a minute whether `SCAN_INTERVAL` minutes have passed since `last_scan` and starts a scan of all shares. Stored times are naive server-local strings (`now_str()`, `encode.sh` `date`); the `localtime` filter renders them as `<time datetime=…>` with the server's UTC offset, and `static/time.js` shows them in the viewer's time zone. The Status page's queue comes from `encoding.queue_items()` (entries before `current.txt`'s number are finished, using their result; `/status/live` is a small JSON snapshot polled by `static/status.js`; `/status/queue` returns pages of rows). The Files page never renders the whole library. `/files` renders the top level only, and `/files/folder?path=…` returns one folder level (`_level.html`: subfolders plus the first `PAGE_SIZE` files) (subfolders paged by `FOLDER_PAGE`; with `dirs=N` only the next folders plus a new "Show more folders" button, which `files.js` swaps in) or, with `rows=1`, the next page of file rows (`_file_rows.html`, next URL in the `X-Next` header). `static/files.js` fetches a folder when it's opened. `tree_level()` also counts per folder `optimized` (status `h265`), `unknown` (not probed) and `other`, which `_level.html` shows as the folder's status bar and badge. Sorting: column headers cycle ascending → descending → off via `next_sort()`; `sort_args()` reads `sort`/`dir` (`{}` = default order), `tree_level()` orders files with `SORT_FIELDS` (and folders with `FOLDER_SORT`) via `sort_rows()` (missing values last, name as tiebreak); folder URLs carry filters and sort (`link_args`). The Status queue sorts the same way on the server (`QUEUE_SORT`); small fully rendered tables use `<table data-sortable>` with `static/tablesort.js` (cells may carry a raw `data-sort` value). `files.js` keeps open folder paths in `sessionStorage` and reopens them after reloads; it also keeps each loaded folder's HTML there, tagged with the page's `data-version` (hash of `enc.db_version()` and the queue), so reopened folders need no request until the data changes (↻ reloads bypass it). Folder URLs are built in JS from `data-folder-url` + `data-folder-query` (kept out of every row to slim the markup). `sort_state()` remembers the Files and queue sort in the Flask session (`sort=none` = off); `tablesort.js` stores client sorts in `localStorage`. `cached(name, version, compute)` keeps `library_summary()` and the unfiltered top level until `enc.db_version()` changes; `tree_level()` and `/stats` aggregate in SQL. `compress()` gzips HTML/JSON responses over 2 KB; `squeeze()` collapses whitespace between tags in folder responses. Closed folder rows use `content-visibility: auto`, so offscreen rows skip layout. Status housekeeping: `POST /queue/remove` (`key` values or `all=1`) and `POST /results/clear`. All filtering goes through `filter_sql()`: text via a Unicode-aware `pylower()`, codec as a list of names, `unknown` or the `CODEC_GROUPS`, and status. A folder path is turned into a key range (`key >= 'p/' AND key < 'p0'`) that uses the primary-key index. `POST /queue` takes `key` values (validated against the `files` table, so no arbitrary paths reach `encode.sh`) and `folder` values, which are expanded on the server with the submitted filters. It then calls `encoding.write_batch()`, the same function the CLI `queue` uses. In the browser, a ticked folder disables the inputs below it, so they aren't submitted twice. `/stats` computes all chart data in Python (`chart()`), and the templates render it as plain HTML/CSS bars, without a chart library or CDN. The chart colors are validated for color-blind safety in light and dark mode (`--viz-*` tokens in `static/style.css`), so re-validate them if they change. Codec color coding everywhere follows `codec_class()`: `encoding.OPTIMIZED_CODECS` → green, other → orange, not probed → gray. Settings are written back into `config.env` (`write_config`), keeping comments. `FORBIDDEN_RE` blocks anything zsh would execute when sourcing the file. All POSTs need a session CSRF token. The session key is in `DATA_DIR/.secret_key`. There's no login yet (TODO P3), so it binds to 127.0.0.1 by default.
- **Docker**: `docker-entrypoint.sh` creates `config.env` from the template, runs the worker loop (every 30 s, if jobs exist and `WORKER_ENABLED` is 1; the env var overrides `config.env`), and then `exec`s the web UI. The image is Debian 13 with HandBrakeCLI 1.9.x, which maps the preset's `ca_aac` to `av_aac` on its own. `docker-compose.hybrid.yml` bind-mounts the native install folder as `/data`, forces `WORKER_ENABLED=0` (the cross-VM `.lock` PID check would not prevent two workers), and moves the DB to its own volume (`ENCODING_DB_FILE`), because SQLite must not be shared across the Docker VM boundary.

**SQLite (`encoding.db`, WAL)**, schema in `SCHEMA`:
- Tables:
  - `meta`: `last_offer` JSON, `last_scan`
  - `baseline`, `skipped`, `known`
  - `results`: imported from `results.tsv` on every `load_results()`
  - `files`: inventory with ffprobe fields and `status` ∈ `STATUS_LABELS`
- `load_state()`/`save_state()` keep the old `state` dict interface.
- A v1 `state.json` is imported once by `migrate_state_json()` and renamed to `state.json.migrated`.
- Changing the schema or these meanings is a MAJOR change.

macOS quirk: when launchd starts `encode.sh`, `/bin/mv` and `/bin/rm` get "Operation not permitted" on the NFS library/output share, while Python's `os`/`shutil` calls work (verified with `launchctl submit`). Every file operation on the share in `encode.sh` must use the `fs` helper (Python); plain `mv`/`rm` are only fine for local files (queue, logs, lock).

Shared file contracts (change both sides together):
- **Keys**: every file is identified as `share<N>/<rel path>`, where N is the share number from `SHARE_N`. Names (`SHARE_N_NAME`) are display-only. The numbers are part of stored keys, so they must never be renumbered or reused: the web UI comments out removed shares, and `next_share_number()` skips any number that appears in `config.env`, even commented out. Batch files contain one entry per line: `key` or `key<TAB>preset id` (an override chosen when queueing). `encode.sh` looks up the prefix in `ROOTS` and picks the preset as override, then `SHARE_N_PRESET`, then `DEFAULT_PRESET`; `status.sh` and `queued_entries()` read the same format. Keys from version 1.x (`Filme/`) and 2.x (`Movies 2/`) are translated by `legacy_key()` with the `v2_prefix_map` stored in the `meta` table, whenever old `state.json` or `results.tsv` lines are imported.
- **`logs/results.tsv`**: `date \t STATUS \t key`, status ∈ OK/FAIL/MISSING/EXISTS. Written by `encode.sh`, imported into the DB incrementally (byte offset in `meta` `results_offset`, reset when the file shrinks; `clear_results()` truncates it); `encoding.py` uses the last entry per key (e.g. to flag prior failures).
- **`cancelled.txt`** (data folder): `batch file name \t key` per line, entries of the current run removed in the web UI (`remove_queued()`; waiting entries in `queue/` are instead rewritten after moving the batch into `.queue-edit/`). `encode.sh` skips matching entries (still counting them in `i`/`total`) and deletes the file when the run ends; `queue_items()` shows them as `removed`, `queued_entries()` leaves them out.
- **`logs/current.txt`**: `key \t i \t total \t epoch` for the file being encoded. **`logs/progress.log`** is HandBrakeCLI stdout. Both are parsed by regex (`Encoding: ..., N %`, `avg N fps`, `ETA XhYmZs`) in `encoding.current_progress()` and in `status.sh`.

Scan classification order in `_scan()` (first match wins):
1. already optimized: name matches `x265|h265|hevc|FuN`, or ffprobe reports a codec in `OPTIMIZED_CODECS` (hevc, av1, vvc); stored as status `h265`
2. bad release type: `BAD_RELEASE_RE`, matched only as a separator-delimited token in the stem
3. stem already present in `_ENCODED`
4. already queued
5. skipped
6. legacy baseline
7. offered

The offered list's numbering is stored as `last_offer` and stays stable until the next scan; `queue` and `skip` refer to those numbers. Skips are keyed by full path, so a renamed file reappears.

## Configuration

Machine-specific settings live in **`config.env`** next to the scripts. It is gitignored and created from the committed template `config.example.env`, which documents every key.
- **Format:** shell `KEY="value"` lines. `encode.sh` `source`s the file; `encoding.py` parses it with `load_config()` (`shlex`, then `$VAR` expansion) and `apply_config()` turns it into `ROOT_CANDIDATES`, `ENCODED_CANDIDATES` and `NOTE_DIR_CANDIDATES`.
- **Path lists:** folder keys may list several paths separated by `:`. Both scripts use the first path that exists (`first_existing`, implemented on each side). This keeps the `~/media/...` fallback paths working when the tool runs inside a Claude environment.
- **Missing config:** both scripts stop with an error if `config.env` is missing or incomplete. `version` works without it.
- **New settings:** add the key to `config.example.env`, to the README's Configuration table and to `SETTINGS` in `webui.py` if it should be editable there, and read it in both scripts if both need it.
- **Path lists in the template:** folder defaults list the macOS path first and then the container path (`/media/<folder>`, `/encoded`, `/notes`), so one file works natively and in Docker.

| Setting | Where |
|---|---|
| Shares / output / Obsidian folders | `config.env` `SHARE_N` (+ `SHARE_N_NAME`, `SHARE_N_PRESET`), `ENCODED_DIR`, `NOTE_DIR` |
| Encoding settings (codec, RF, audio, crop, container) | presets in `presets/` (web UI Presets page); `preset.json` is only the bundled first preset |
| Preset selection | queue override → `SHARE_N_PRESET` → `config.env` `DEFAULT_PRESET` |
| Automatic rescan, parallel ffprobe calls | `config.env` `SCAN_INTERVAL` (minutes, 0 = off), `FFPROBE_WORKERS` (empty = CPUs) |
| Per-minute Obsidian status note during encoding | `config.env` `STATUS_NOTE_ENABLED` |
| Encode inside the Docker container | `config.env` `WORKER_ENABLED`, overridden by env `WORKER_ENABLED` |
| Data folder, DB file, web address | env `ENCODING_DATA_DIR`, `ENCODING_DB_FILE`, `WEB_HOST`, `WEB_PORT` |
| Host folders mounted into Docker | `.env` (template `.env.example`): `HOST_MEDIA_DIR` (→ `/media`), `HOST_ENCODED_DIR`, `HOST_NOTE_DIR`, `HOST_APP_DIR` |
| "Already H.265" detection (filename only, stream not probed) | `encoding.py` `HEVC_RE` |
| Excluded release types | `encoding.py` `BAD_RELEASE_RE` |
| Scanned file extensions | `encoding.py` `VIDEO_EXTS` |
| Retry timing | plist `ThrottleInterval` (30 s); `encode.sh` sleeps 300 s if NAS, HandBrakeCLI or `config.env` is missing |

Only user choices change at runtime: skipped files and the legacy baseline, stored in `encoding.db`.

Both plists (`local.plex-encoding.plist` worker, `local.plex-encoding-web.plist` web UI) are templates: their `/Users/YOUR_USER/scripts/encoding` paths are replaced with the install folder by the `sed` command in the README when they are copied to `~/Library/LaunchAgents`.

The bundled preset is named `1080p x265 (Ger/Eng) RF20`, although its actual `VideoQualitySlider` is 22.

Local files (`config.env`, `.env`, `.venv/`) and runtime artifacts (`encoding.db*`, `.secret_key`, `.scan.lock`, `state.json*`, `queue/`, `processing/`, `done/`, `logs/`, `.lock`, `replace/`, `presets/`, `cancelled.txt`, `.queue-edit/`, `.handbrake-cache.json`) are created in the data folder and are gitignored (and excluded from the Docker image via `.dockerignore`).
