# Plex Encoding Automation (H.264 → H.265)

Semi-automatic conversion of movies and TV episodes in a Plex library from H.264 (and other older codecs) to H.265/HEVC with HandBrakeCLI. You decide which files get encoded; the tool finds candidates, queues them, encodes them in the background and never touches the originals.

## Contents

- [What it does](#what-it-does)
- [Choose a setup](#choose-a-setup)
- [Setup A: native on macOS](#setup-a-native-on-macos)
- [Setup B: Docker](#setup-b-docker)
- [Setup C: Docker + native encoding on a Mac](#setup-c-docker--native-encoding-on-a-mac)
- [Daily use](#daily-use)
- [Web UI](#web-ui)
- [Configuration](#configuration)
- [Files and data](#files-and-data)
- [Updating from version 1.x](#updating-from-version-1x)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

## What it does

1. **Scan** the library (a movies folder and a TV shows folder). Each video is read with ffprobe (codec, resolution, duration, bitrate) and sorted:
   - already H.265: `x265`, `H.265`, `HEVC` or `FuN` in the name, or an HEVC video track according to ffprobe
   - poor release types, filtered out: LD, MD, TS, Telesync, CAM, HDCAM, TC, Line/Mic Dubbed
   - already encoded and waiting for your review in the output folder
   - already in the queue
   - skipped by you, or part of the legacy baseline from the previous script
   - everything else: **offered** as a numbered list
2. **Choose** which numbers to encode. This writes a batch file into `queue/`.
3. **Encode**: a worker picks up the batch and runs HandBrakeCLI with your preset (`preset.json`). It writes to `<output>/.inprogress/` first and moves the file to the output folder only when the encode succeeded.
4. **Review** the finished files yourself and move them into the library (e.g. renaming them to x265). Until then, a rescan shows them as "waiting for review".

Scans can also update an Obsidian note with the to-do list. Steps 1 and 2 can be automated, e.g. with a scheduled Claude task that scans regularly and asks for your selection via push notification.

| File | Purpose |
|---|---|
| `encoding.py` | Command line: scan, queue, skip, status. Stores its data in SQLite (`encoding.db`). |
| `encode.sh` | Worker: encodes everything in `queue/` with HandBrakeCLI |
| `webui.py`, `templates/`, `static/` | Web UI on port 7979 |
| `config.example.env` | Template for `config.env`, the local settings |
| `preset.json` | HandBrake preset (x265, RF 22, MKV, AAC 5.1) |
| `local.plex-encoding.plist` | launchd agent (macOS) that starts `encode.sh` when a batch appears in `queue/` |
| `local.plex-encoding-web.plist` | launchd agent (macOS) that keeps the web UI running |
| `status.sh` | Live progress view in the terminal |
| `Dockerfile`, `docker-compose.yml`, `docker-compose.hybrid.yml`, `.env.example`, `docker-entrypoint.sh` | Docker setup |

## Choose a setup

| | A: native on macOS | B: Docker | C: Docker + native encoding |
|---|---|---|---|
| Runs on | macOS | macOS (Docker Desktop) | macOS (Docker Desktop) |
| Encoding | HandBrakeCLI on the Mac, all CPU cores, Apple AAC | HandBrakeCLI 1.9 (Linux) in Docker's VM, AAC via `av_aac`, limited to the CPUs Docker Desktop may use | Native, like A |
| Web UI, scan | On the Mac (Python venv) | In the container | In the container |
| Install effort | Homebrew packages, Python venv, launchd agents | Docker Desktop and two config files | Both A and B |

If you're unsure, use **B** to try it out, or **A** for the fastest encoding. Other operating systems aren't documented yet.

## Setup A: native on macOS

### Requirements

- macOS (uses launchd, `caffeinate`, zsh, and Apple's AAC encoder `ca_aac` from the preset)
- Python 3.9 or later (`python3 --version`; the Xcode Command Line Tools or Homebrew provide it)
- [Homebrew](https://brew.sh) for HandBrakeCLI and ffmpeg (or install them manually)
- The library mounted on the Mac, e.g. as an SMB share from a NAS
- Enough free disk space in the output folder; encoded files stay there until you move them

### 1. Install the tools

```bash
brew install handbrake ffmpeg      # HandBrakeCLI and ffprobe
HandBrakeCLI --version
ffprobe -version | head -1
```

The HandBrake *app* alone isn't enough; the command-line version is a separate install. It's also available from [handbrake.fr](https://handbrake.fr/downloads2.php). Without ffmpeg the tool still works, but codec, resolution and bitrate stay empty, and only the file name decides whether a file is already H.265.

### 2. Mount the library

Connect to the share in Finder (Go → Connect to Server, e.g. `smb://nas/Media`). It then appears under `/Volumes`, e.g. `/Volumes/Media/Movies` and `/Volumes/Media/Shows`. To reconnect it after a restart, add the share to System Settings → General → Login Items.

### 3. Get the code

```bash
mkdir -p ~/scripts
git clone <repository-url> ~/scripts/encoding     # or download and unpack the code there
cd ~/scripts/encoding
```

All further commands run in this folder.

### 4. Create the configuration

```bash
cp config.example.env config.env
open -e config.env
```

Set at least `MOVIES_DIR`, `SHOWS_DIR`, `ENCODED_DIR` and `PRESET_NAME`, and `NOTE_DIR` if you want Obsidian notes. Each key is explained in [Configuration](#configuration). Each folder setting already lists a container path after the `:`; it doesn't hurt in a native setup.

### 5. First scan

```bash
python3 encoding.py scan
```

The first scan reads every file with ffprobe, which can take a while over the network. Later scans only read new or changed files.

### 6. Install the web UI (optional)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 webui.py           # http://127.0.0.1:7979, stop with Ctrl+C
```

### 7. Start the background services

```bash
chmod +x encode.sh status.sh
mkdir -p logs

# Encoding worker: starts as soon as a batch appears in queue/
sed "s|/Users/YOUR_USER/scripts/encoding|$PWD|g" local.plex-encoding.plist \
  > ~/Library/LaunchAgents/local.plex-encoding.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.plex-encoding.plist

# Web UI (only if you did step 6): starts at login and restarts if it stops
sed "s|/Users/YOUR_USER/scripts/encoding|$PWD|g" local.plex-encoding-web.plist \
  > ~/Library/LaunchAgents/local.plex-encoding-web.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.plex-encoding-web.plist
```

The `sed` command fills the plist templates with your install folder.

### 8. Allow access

The first time launchd starts the scripts, macOS may ask whether `zsh`, `HandBrakeCLI` or `python3` may access network volumes or the Downloads folder. Allow it, or grant them access under System Settings → Privacy & Security (Files and Folders, or Full Disk Access). If the worker logs "Library not reachable" even though the share is mounted, missing permissions are the usual cause.

## Setup B: Docker

### Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) for Mac
- The library mounted on the Mac (see [Setup A, step 2](#2-mount-the-library))
- Docker Desktop must be allowed to read the library and the output folder. `/Users` and `/Volumes` are shared by default; check under Settings → Resources → File sharing.
- Under Settings → Resources, give Docker enough CPUs and memory. Encoding only uses what Docker Desktop allows.

### 1. Get the code and configure the paths

```bash
git clone <repository-url> plex-encoding-automation
cd plex-encoding-automation
cp .env.example .env
open -e .env
```

`.env` tells Docker which folders on the Mac to mount into the container:

| `.env` key | Mac folder | Path in the container |
|---|---|---|
| `HOST_MOVIES_DIR` | Movies (mounted read-only) | `/media/Movies` |
| `HOST_SHOWS_DIR` | TV shows (mounted read-only) | `/media/Shows` |
| `HOST_ENCODED_DIR` | Output folder | `/encoded` |
| `HOST_NOTE_DIR` | Optional: Obsidian folder | `/notes` |
| `WEB_PORT` | Optional: port on the Mac, default `7979` | `7979` |

### 2. Start

```bash
docker compose up -d --build
```

On first start the container creates `config.env` in its data volume. Its defaults already point to the container paths above, so usually only `PRESET_NAME` needs checking.

### 3. Open the web UI

Open <http://127.0.0.1:7979>. Under **Settings**, check that each folder has a green ✓. Then click **Scan now**.

### 4. Queue files

Choosing files works on the command line for now:

```bash
docker compose exec encoding python3 encoding.py scan
docker compose exec encoding python3 encoding.py queue 1,3,5-8
```

The worker in the container checks the queue every 30 seconds and starts encoding. Progress appears on the **Status** page.

### Useful Docker commands

```bash
docker compose logs -f                               # container output
docker compose exec encoding zsh status.sh           # live progress in the terminal
docker compose exec encoding cat /data/logs/encode.log   # worker log
docker compose down                                  # stop (data volume stays)
docker compose up -d --build                         # update after pulling new code
```

## Setup C: Docker + native encoding on a Mac

The container runs the web UI and the scans, and the Mac keeps encoding natively with launchd. Use this to get the web UI without installing Python packages on the Mac while keeping native encoding speed.

1. Do [Setup A](#setup-a-native-on-macos) steps 1 to 4 and step 7, but install only the encoding worker (`local.plex-encoding.plist`), not the web UI agent.
2. Make sure each folder setting in `config.env` contains the Mac path and the container path, e.g. `MOVIES_DIR="/Volumes/Media/Movies:/media/Movies"`. The template already does this.
3. Do [Setup B](#setup-b-docker) step 1 and also set `HOST_APP_DIR` in `.env` to the native install folder (e.g. `/Users/you/scripts/encoding`).
4. Start with both compose files:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.hybrid.yml up -d --build
   ```

The container and the Mac then share `config.env`, `queue/` and `logs/`. Batches queued in the container are picked up by launchd on the Mac, and the Status page shows the Mac's progress. The container never encodes in this mode.

The database is in a separate Docker volume, because SQLite must not be used from the Mac and the container at the same time through a shared folder. Run `encoding.py` only inside the container in this setup:

```bash
docker compose -f docker-compose.yml -f docker-compose.hybrid.yml exec encoding python3 encoding.py queue 1,2
```

## Daily use

```bash
python3 encoding.py scan                    # find candidates, numbered list
python3 encoding.py scan --no-probe         # faster, without ffprobe
python3 encoding.py queue all               # queue everything in the list
python3 encoding.py queue 1,2,5-8           # queue by number
python3 encoding.py queue all -3 -7 --skip-rest
                                            # everything except 3 and 7, and hide those two
python3 encoding.py skip 4                  # hide a file permanently (until its name changes)
python3 encoding.py skipped                 # list hidden files
python3 encoding.py unskip 2                # show number 2 of that list again
python3 encoding.py status                  # current progress
python3 encoding.py version
zsh status.sh                               # live view, Ctrl+C ends only the view
```

In Docker, prefix each command with `docker compose exec encoding`. The numbers always refer to the list from the latest scan.

## Web UI

| Page | Content |
|---|---|
| **Files** | All scanned videos as a folder tree with codec, resolution, bitrate, duration, size and status. Totals and the H.265 share at the top. Filters by text, codec and status. |
| **Status** | Current encode with progress, the queue, the latest results and a scan button. Refreshes every 10 seconds. |
| **Settings** | The values from `config.env`, in separate sections (Library, Encoding, Obsidian, Worker). Each folder shows whether it exists. Changes apply immediately. |

There is **no login yet**. The web UI therefore listens on `127.0.0.1` only, and `docker-compose.yml` publishes the port only on `127.0.0.1`. Don't make it reachable from other machines until login is implemented. Values entered in the settings may use `$HOME`, but no other `$`, quotes or backslashes, because `config.env` is read by the shell.

## Configuration

### `config.env`

All machine-specific settings live in `config.env` in the data folder. It is created from `config.example.env` and stays out of git. You can edit it with a text editor or in the web UI.

| Key | Meaning |
|---|---|
| `MOVIES_DIR`, `SHOWS_DIR` | Movie and TV show folders of the library (read only) |
| `ENCODED_DIR` | Output folder for finished files, created if missing |
| `NOTE_DIR` | Obsidian folder for "Encoding To-Do.md" and "Encoding Status.md". Leave empty to skip notes. |
| `PRESET_FILE` | HandBrake preset file. Leave empty to use `preset.json` next to the scripts. |
| `PRESET_NAME` | The preset's internal name (`PresetName` in the JSON) |
| `STATUS_NOTE_ENABLED` | `1` updates the status note every minute while encoding; `0` turns it off |
| `WORKER_ENABLED` | Docker only: `1` encodes in the container, `0` only runs web UI and scans |

Write values in double quotes and use `"$HOME"` rather than `~`. A folder setting can list several paths separated by `:`, and the first one that exists is used. That way one `config.env` works on the Mac and in the container.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ENCODING_DATA_DIR` | the program folder (Docker: `/data`) | Where `config.env`, `encoding.db`, `queue/`, `logs/` etc. live |
| `ENCODING_DB_FILE` | `$ENCODING_DATA_DIR/encoding.db` | Database file. It must be on a local disk, not on a network share. |
| `WEB_HOST`, `WEB_PORT` | `127.0.0.1`, `7979` (Docker: `0.0.0.0` inside the container) | Address of the web UI; also `webui.py --host/--port` |
| `WORKER_ENABLED` | not set | Docker only: overrides `WORKER_ENABLED` from `config.env` (set to `0` by `docker-compose.hybrid.yml`) |

### Using your own preset

Export a preset from the HandBrake app (Presets → Export). Point `PRESET_FILE` to it, or replace `preset.json`. Then set `PRESET_NAME` to its `PresetName` value; the web UI suggests the names found in the file. In Docker, put the file into the data volume or mount it, and use its path inside the container.

## Files and data

Everything the tool creates lives in the data folder (the program folder, or `/data` in Docker):

| Path | Content |
|---|---|
| `config.env` | Your settings |
| `encoding.db` | SQLite database: file inventory with ffprobe data, skipped files, legacy baseline, last numbered list, encode results |
| `queue/`, `processing/`, `done/` | Batch files: waiting, being encoded, finished |
| `logs/encode.log` | Worker log |
| `logs/results.tsv` | One line per encoded file (OK, FAIL, MISSING, EXISTS); copied into the database |
| `logs/fail-*.log` | HandBrake log of a failed encode |
| `logs/launchd.log`, `logs/webui.log` | Output of the launchd agents (Setup A) |

## Updating from version 1.x

1. Let the queue run empty first (`queue/` and `processing/` contain no batch files). Batches from 1.x use the old German key prefixes, which the new worker no longer accepts.
2. Create `config.env` from `config.example.env` and copy your paths and preset name into it (they used to be in `encoding.py` and `encode.sh`). The movie and TV show folders are now set with `MOVIES_DIR` and `SHOWS_DIR`.
3. On the first run, `state.json` is imported into `encoding.db` automatically and renamed to `state.json.migrated`. Your skipped files, legacy baseline and last list are kept, and old entries in `logs/results.tsv` are imported too.
4. Scripts that call the tool need two changes: `queue alle` is now `queue all`, and `scan --json` uses English field names (`offer`, `queued`, `awaiting_review`, …).
5. The launchd agent is now named `local.plex-encoding`. Remove the old one and install the new one as in [Setup A, step 7](#7-start-the-background-services):
   ```bash
   launchctl bootout gui/$(id -u)/<old label>
   rm ~/Library/LaunchAgents/<old plist file>
   ```

## Troubleshooting

| Problem | Check |
|---|---|
| `config.env is missing` | Create it: `cp config.example.env config.env` |
| `Movies, Shows not reachable. Is the NAS mounted?` | Is the share mounted? Do the paths in `config.env` (or `.env` for Docker) match? |
| Worker logs `Library not reachable` although the share is mounted | macOS privacy permissions for launchd jobs, see [Setup A, step 8](#8-allow-access) |
| `HandBrakeCLI not found` | Install HandBrakeCLI (not just the app) to `/opt/homebrew/bin` or `/usr/local/bin` |
| Codec and bitrate stay empty | Install ffmpeg (`brew install ffmpeg`), then scan again |
| An encode failed | `logs/fail-<date>.log` contains HandBrake's log |
| Docker: folders show ✗ in the settings | Paths in `.env` and Docker Desktop file sharing |
| `A scan is already running.` | Wait for the other scan (web UI or terminal) to finish |

## Uninstalling

```bash
launchctl bootout gui/$(id -u)/local.plex-encoding
launchctl bootout gui/$(id -u)/local.plex-encoding-web
rm ~/Library/LaunchAgents/local.plex-encoding*.plist
docker compose down -v          # Docker: also deletes the data volume
```

Then delete the program folder. Your library is never modified, and encoded files stay in the output folder.
