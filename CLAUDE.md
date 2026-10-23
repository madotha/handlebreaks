# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Maintaining this file and TODO.md

- Keep this CLAUDE.md in sync with the tool: whenever a change affects commands, architecture, file contracts or configuration, update the relevant section in the same change.
- `TODO.md` tracks all planned work, general and personal. It is gitignored and stays local, so it may be missing in a fresh clone. Sort every new TODO into one of its existing categories (Repository & Infrastructure, Configuration, Features, Bugs & Fixes, Documentation); add a new category only if none fits. Check items off (`- [x]`) when done.

## Overview

Semi-automatic H.264 → H.265 (x265) re-encoding of a Plex library on a Mac, using HandBrakeCLI. No build system, dependencies, linter or tests: plain Python 3 (stdlib only) plus zsh scripts. All code comments, CLI output, and the README are in **German**; keep it that way when editing.

## Commands

```bash
python3 encoding.py scan [--json]            # scan NAS, refresh numbered list + Obsidian note
python3 encoding.py queue alle -3 -7 --skip-rest   # queue by number from last scan (also: 1,2,5-8)
python3 encoding.py skip 4 | skipped | unskip 2
python3 encoding.py status
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

There is no config file or CLI options beyond the subcommands; everything is a constant in the source:

| Setting | Where |
|---|---|
| Library / output / Obsidian paths | `encoding.py` `ROOT_CANDIDATES`, `ENCODED_CANDIDATES`, `NOTE_DIR_CANDIDATES`; `encode.sh` `FILME`, `SERIEN`, `OUT`; all paths in the plist |
| Encoding settings (codec, RF, audio, crop, container) | `preset.json` (exported from the HandBrake app) |
| Preset selection | `encode.sh` `PRESET_NAME` |
| "Already H.265" detection (filename only, stream not probed) | `encoding.py` `HEVC_RE` |
| Excluded release types | `encoding.py` `BAD_RELEASE_RE` |
| Scanned file extensions | `encoding.py` `VIDEO_EXTS` |
| Per-minute Obsidian status note during encoding | `encode.sh` `STATUS_NOTE_ENABLED` (currently `0` = off) |
| Retry timing | plist `ThrottleInterval` (30 s); `encode.sh` sleeps 300 s if NAS or HandBrakeCLI is missing |

Only user choices change at runtime: skipped files and the legacy baseline, stored in `state.json`.

Paths are hard-coded example defaults (placeholders like `YOUR_USER`, `YOUR_VAULT`). `encoding.py` tries each `*_CANDIDATES` list in order: real macOS paths first, then `~/media/...` mounts used when run from a Claude environment. `encode.sh` and the plist (`/Users/YOUR_USER/scripts/encoding/...`) have their own copies, so path changes must be made in all three places.

`PRESET_NAME` must equal `PresetName` inside `preset.json` (currently `1080p x265 (Ger/Eng) RF20`, although the actual `VideoQualitySlider` is 22).

Runtime artifacts (`state.json`, `queue/`, `processing/`, `done/`, `logs/`, `.lock`) are created next to the scripts and are not part of the repo.
