# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
