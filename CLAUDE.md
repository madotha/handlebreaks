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
- To release, bump `__version__`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]` section, commit, and tag `vX.Y.Z` (annotated).

## Overview

Semi-automatic H.264 → H.265 (x265) re-encoding of a Plex library on a Mac, using HandBrakeCLI. No build system, dependencies, linter or tests: plain Python 3 (stdlib only) plus zsh scripts. Code comments, CLI output and the Obsidian note are in **German**; keep them that way when editing. The README is in **English** and lists the requirements, so update it when they change.

## Commands

```bash
python3 encoding.py scan [--json]            # scan NAS, refresh numbered list + Obsidian note
python3 encoding.py queue alle -3 -7 --skip-rest   # queue by number from last scan (also: 1,2,5-8)
python3 encoding.py skip 4 | skipped | unskip 2
python3 encoding.py status
python3 encoding.py version                  # print __version__ (also --version, -V)
python3 encoding.py import-legacy <cache.json> <note.md>   # one-time migration from old encoding_todo.py
zsh encode.sh                                # run the worker manually (normally started by launchd)
zsh status.sh                                # live terminal progress view
```

Running `scan` or `queue` has real side effects (writes `state.json`, the Obsidian note, and `queue/` batches that launchd immediately picks up and starts encoding). Don't run them casually while testing.

## Architecture

Two independent processes coordinate only through files in the script directory (`BASE`):

- **`encoding.py`** (controller): walks `Filme/` and `Serien/` on the NAS, classifies each video, persists state in `state.json`, writes `Encoding To-Do.md` into an Obsidian vault, and on `queue` writes a `queue/batch-<timestamp>.txt` (written to a temp file then `os.replace`d so launchd never sees a partial file).
- **`encode.sh`** (worker): started by the launchd agent `local.plex-encoding.plist` via `QueueDirectories` on `queue/`. Moves batches `queue/ → processing/ → done/`, encodes each entry into `_ENCODED/.inprogress/` and only moves to `_ENCODED/` on success. Uses a `mkdir`-based `.lock` (with stale-PID cleanup) for single-instance, `caffeinate` to keep the Mac awake, and exits/sleeps if the NAS or HandBrakeCLI is missing so launchd retries. Originals on the NAS are never modified.

Shared file contracts (change both sides together):
- **Keys**: every file is identified as `Filme/<rel path>` or `Serien/<rel path>`. Batch files contain one key per line; `encode.sh` maps the prefix to `$FILME`/`$SERIEN`.
- **`logs/results.tsv`**: `date \t STATUS \t key`, status ∈ OK/FAIL/MISSING/EXISTS. `encoding.py` uses the last entry per key (e.g. to flag prior failures).
- **`logs/current.txt`**: `key \t i \t total \t epoch` for the file being encoded; **`logs/progress.log`**: HandBrakeCLI stdout, parsed by regex (`Encoding: ..., N %`, `avg N fps`, `ETA XhYmZs`) in both `encoding.py` and `status.sh`.

Scan classification order in `scan()` (first match wins): HEVC name match (`x265|h265|hevc|FuN`) → bad release type (`BAD_RELEASE_RE`, matched only as a separator-delimited token in the stem) → stem already present in `_ENCODED` → already queued → skipped → legacy baseline → offered. The offered list's numbering is stored as `state["last_offer"]` and stays stable until the next scan; `queue`/`skip` refer to those numbers. Skips are keyed by full path, so a renamed file reappears.

## Configuration

Machine-specific settings live in **`config.env`** next to the scripts. It is gitignored and created from the committed template `config.example.env`, which documents every key.
- **Format:** shell `KEY="value"` lines. `encode.sh` `source`s the file; `encoding.py` parses it with `load_config()` (`shlex`, then `$VAR` expansion) and `apply_config()` turns it into `ROOT_CANDIDATES`, `ENCODED_CANDIDATES` and `NOTE_DIR_CANDIDATES`.
- **Path lists:** folder keys may list several paths separated by `:`. Both scripts use the first path that exists (`first_existing`, implemented on each side). This keeps the `~/media/...` fallback paths working when the tool runs inside a Claude environment.
- **Missing config:** both scripts stop with an error if `config.env` is missing or incomplete. `version` works without it.
- **New settings:** add the key to `config.example.env` and the README's Configuration table, and read it in both scripts if both need it.

| Setting | Where |
|---|---|
| Library / output / Obsidian folders | `config.env` `FILME_DIR`, `SERIEN_DIR`, `ENCODED_DIR`, `NOTE_DIR` |
| Encoding settings (codec, RF, audio, crop, container) | `preset.json` (exported from the HandBrake app), or the file set in `config.env` `PRESET_FILE` |
| Preset selection | `config.env` `PRESET_NAME` |
| Per-minute Obsidian status note during encoding | `config.env` `STATUS_NOTE_ENABLED` |
| "Already H.265" detection (filename only, stream not probed) | `encoding.py` `HEVC_RE` |
| Excluded release types | `encoding.py` `BAD_RELEASE_RE` |
| Scanned file extensions | `encoding.py` `VIDEO_EXTS` |
| Retry timing | plist `ThrottleInterval` (30 s); `encode.sh` sleeps 300 s if NAS, HandBrakeCLI or `config.env` is missing |

Only user choices change at runtime: skipped files and the legacy baseline, stored in `state.json`.

The plist is a template: its `/Users/YOUR_USER/scripts/encoding` paths are replaced with the install folder by the `sed` command in the README when it is copied to `~/Library/LaunchAgents`.

`PRESET_NAME` must equal `PresetName` inside the preset file (the bundled one is `1080p x265 (Ger/Eng) RF20`, although its actual `VideoQualitySlider` is 22).

Local files (`config.env`) and runtime artifacts (`state.json`, `queue/`, `processing/`, `done/`, `logs/`, `.lock`) are created next to the scripts and are not part of the repo.
