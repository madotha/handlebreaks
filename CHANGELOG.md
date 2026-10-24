# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Automatic rescan: `SCAN_INTERVAL` (minutes, default `0` = off; Settings → Scans) rescans all shares once the last scan is that old. It runs in the web UI process, and the Status page shows when the next one is due.
- Live queue on the Status page: every queued entry with its state (waiting, encoding, done, failed, missing, already exists) and preset. Progress and counts update every 3 seconds without reloading the page, the list is paged (100 entries), filterable by state, and reloads when the next file starts or on "Refresh", so it stays fast with thousands of queued files.
- Docker: `TZ` in `.env` sets the container's time zone (default UTC); the image now includes tzdata.
- Folder status in the Files page tree: every folder and share shows how many of its files are already optimized ("36 of 48 optimized"), a bar split into optimized (green), other codecs (orange) and not probed (gray), and a percentage badge that is green only when the whole folder is optimized. The counts follow the active filters.

### Changed

- Times in the web UI are shown in the viewer's time zone (as `<time>` elements converted in the browser). Before, they appeared in the server's time zone, which is UTC in a Docker container by default.

## [3.0.0] - 2026-10-24

### Added

- Presets page in the web UI. You can create presets from HandBrake's built-in presets, as copies, or by importing HandBrake export files (including files with several presets or folders). Each preset can be edited in a form for the common settings or as full JSON, exported, made the default, or deleted. HandBrakeCLI checks a preset before it's saved. The editor's choices (encoders, encoder speeds, containers, mixdowns) and the built-in list come from the installed HandBrakeCLI, so they follow its version.
- A preset per share (`SHARE_N_PRESET`) and a default preset (`DEFAULT_PRESET`).
- A preset override when queueing: a **Preset** choice next to "Queue for encoding" on the Files page, and `queue … --preset <id>` in the CLI. The Status page shows overrides in the queue.
- Scanning single shares: "Initial scan" for new shares and "Full rescan" (re-reads every file with ffprobe) for existing ones under Settings → Library, and `scan --share N [--force]` in the CLI. Other shares keep their data, and the numbered list still covers the whole library. The Files page lists shares that haven't been scanned yet.
- Output files get the extension of the preset's container (`.mkv`, `.mp4`, `.webm`).
- Queue files from the web UI: on the Files page, select single files or whole folders and click "Queue for encoding". Any scanned file can be queued, not only the offered ones.
- Stats page in the web UI with key figures and charts of the distribution by codec (count and storage), bitrate, file size, resolution, library section and status. Each chart has a table view.
- Several library folders per section: `MOVIES_DIR_2`, `MOVIES_DIR_3`, … and `SHOWS_DIR_2`, … (also editable under Settings → Library). Each folder gets its own key prefix ("Movies 2", …), so files with the same relative path in different folders never clash, and movies and TV shows stay separate. Existing installs keep their keys.
- Codec filter on the Files page with multiple selection: any of the codecs found, plus "All optimized codecs" and "All other codecs".
- Readable codec names in the web UI (e.g. "H.264 (AVC)") and color-coded codec badges: green for optimized codecs (H.265, AV1, H.266), orange for all others.

### Changed

- **Breaking:** the library is now a list of shares (`SHARE_1`, `SHARE_2`, … with `SHARE_N_NAME`) instead of movie and TV show folders, and presets live in a preset store (`presets/` in the data folder) instead of `PRESET_FILE`/`PRESET_NAME`. A version 2.x `config.env` and all stored data (database, queue, results) are migrated automatically on the first run; the old file is kept as `config.env.v2.bak`.
- **Breaking (Docker):** `HOST_MOVIES_DIR`/`HOST_SHOWS_DIR` are replaced by `HOST_MEDIA_DIR`, mounted read-only at `/media`.
- Batch lines can carry a preset id after a tab (`key<TAB>preset`); `encode.sh`, `status.sh` and the CLI read both forms.
- The "Storage by library folder" chart is now "Storage by share".
- Files whose video track is AV1 or H.266 (VVC) now count as already optimized and are no longer offered for encoding, just like H.265.
- Files queued with `queue` (CLI or web UI) show as queued right away, without waiting for the next scan.
- The Files page loads the folder tree on demand: it shows the top folders, loads a folder's content when it's opened, and lists 200 files at a time ("Show more"). Filters and totals are computed in the database. Large libraries (tested with about 72,000 files) no longer produce a page of tens of megabytes. Folders and files are sorted naturally ("Season 2" before "Season 10"), and extra library folders show their folder name next to "Movies 2" etc.
- Ticking a folder queues all its files that match the active filters without loading them; queueing more than 200 files asks for confirmation.
- The "Storage by library section" chart is now "Storage by library folder" and shows each folder separately.
- Short file names in the CLI, notes and web UI show the folder for extra library folders, e.g. "Movies 2: Film › Film.mkv".
- Batch files get microseconds in their name, so two batches written in the same second can't overwrite each other.

## [2.0.0] - 2026-10-24

### Added

- Web UI on port 7979 (`webui.py`, Flask + waitress), with three pages:
  - **Files:** a folder tree of all scanned files with codec, resolution, bitrate, duration, size and status, plus filters.
  - **Status:** the current encode, the queue, recent results and a scan button.
  - **Settings:** the values from `config.env` in separate sections, editable in the browser.
  
  It listens on 127.0.0.1 only, because there's no login yet. Forms are protected against cross-site requests.
- SQLite database `encoding.db` for skipped files, the legacy baseline, the last numbered list, encode results and a file inventory.
- ffprobe reads codec, resolution, duration and bitrate during `scan`, only for new or changed files. `scan --no-probe` skips it.
- Docker setup: `Dockerfile` (Debian 13, HandBrakeCLI 1.9, ffmpeg), `docker-compose.yml`, and `docker-compose.hybrid.yml` for running the web UI and scans in Docker while the Mac keeps encoding natively. Encoding inside the container can be turned off with `WORKER_ENABLED`.
- launchd template `local.plex-encoding-web.plist`, which runs the web UI natively on macOS.
- Environment variables `ENCODING_DATA_DIR` and `ENCODING_DB_FILE` to keep data outside the program folder.

### Changed

- **Breaking:** `state.json` is replaced by `encoding.db`. An existing `state.json` is imported on first run and renamed to `state.json.migrated`.
- Files whose video track is HEVC according to ffprobe now count as already encoded, even without x265/HEVC in the name.
- The Python requirement rises to 3.9 or later, because of the web UI.
- `encode.sh` skips `caffeinate` where it isn't available (Linux).
- **Breaking:** everything is now in English: CLI output, log messages, the web UI, the Obsidian notes and all comments. This changes several names:
  - The internal key prefixes `Filme/` and `Serien/` are now `Movies/` and `Shows/`, and the settings are `MOVIES_DIR` and `SHOWS_DIR`. Old keys in `state.json` and `logs/results.tsv` are converted on import. Batches still queued from 1.x are not converted, so let the queue run empty before updating.
  - `queue alle` is now `queue all`.
  - `scan --json` uses English field names.
- **Breaking:** machine-specific settings now live in a local `config.env`, which is gitignored and created from the committed template `config.example.env`. This covers the library, output and Obsidian paths, the preset file and name, and `STATUS_NOTE_ENABLED`. `encoding.py` and `encode.sh` stop with an error if `config.env` is missing.
- A setting can list several folders separated by `:`. The first folder that exists is used, in `encode.sh` as well as in `encoding.py`.
- The launchd plist is now a template. Its paths are filled in when it is copied to `~/Library/LaunchAgents` (see README).

## [1.0.0] - 2026-10-23

First versioned release.

### Added

- `encoding.py` with the subcommands `scan`, `queue`, `skip`, `skipped`, `unskip`, `status` and `import-legacy`.
  - It finds non-H.265 videos on the NAS and writes the Obsidian note `Encoding To-Do.md`.
  - It filters out bad release types, already encoded files, queued files, skipped files and the legacy baseline.
- `encode.sh`, the queue worker:
  - It encodes with HandBrakeCLI and `preset.json` via `_ENCODED/.inprogress/`.
  - It logs each result to `logs/results.tsv` and allows only one running instance.
- launchd agent `local.plex-encoding.plist`, which starts `encode.sh` when a batch lands in `queue/`.
- `status.sh` for a live progress view in the terminal.
- Version number in `encoding.py` (`__version__`), shown by `python3 encoding.py version`.
