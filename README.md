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

1. **Scan** the library: one or more **shares** (library folders, e.g. on different NAS shares), each with a name and its own HandBrake preset. Each video is read with ffprobe (codec, resolution, duration, bitrate) and sorted:
   - already optimized: `x265`, `H.265`, `HEVC` or `FuN` in the name, or a video track in H.265 (HEVC), AV1 or H.266 (VVC) according to ffprobe
   - poor release types, filtered out: LD, MD, TS, Telesync, CAM, HDCAM, TC, Line/Mic Dubbed
   - already encoded and waiting for your review in the output folder
   - already in the queue
   - skipped by you, or part of the legacy baseline from the previous script
   - everything else: **offered** as a numbered list
2. **Choose** which files to encode: tick them on the web UI's Files page, or pass their numbers to `encoding.py queue`. Either way a batch file is written into `queue/`.
3. **Encode**: a worker picks up the batch and runs HandBrakeCLI with the preset chosen when queueing, or else the share's preset, or else the default preset. It writes to `<output>/.inprogress/` first and moves the file to the output folder only when the encode succeeded.
4. **Review** the finished files yourself and move them into the library (e.g. renaming them to x265). Until then, a rescan shows them as "waiting for review".

Scans can also update an Obsidian note with the to-do list. Steps 1 and 2 can be automated, e.g. with a scheduled Claude task that scans regularly and asks for your selection via push notification.

| File | Purpose |
|---|---|
| `encoding.py` | Command line: scan, queue, skip, status. Stores its data in SQLite (`encoding.db`). |
| `encode.sh` | Worker: encodes everything in `queue/` with HandBrakeCLI |
| `webui.py`, `templates/`, `static/` | Web UI on port 7979 |
| `config.example.env` | Template for `config.env`, the local settings |
| `plex.py` | Plex integration: analyze an item or scan a folder after a replace |
| `presets.py` | Preset store and what the installed HandBrakeCLI supports (used by the CLI and the web UI) |
| `preset.json` | Bundled HandBrake preset (x265, RF 22, MKV, AAC 5.1), imported as the first preset on first run |
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

Connect to the share in Finder (Go → Connect to Server, e.g. `smb://nas/Media`). It then appears under `/Volumes`, e.g. `/Volumes/Media` with folders like `Movies` and `Shows` inside. NFS shares mounted elsewhere (e.g. under your home folder) work the same way. To reconnect it after a restart, add the share to System Settings → General → Login Items.

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

Set your shares (`SHARE_1`, `SHARE_2`, … with a name each, see [Shares](#shares)) and `ENCODED_DIR`, and `NOTE_DIR` if you want Obsidian notes. Each key is explained in [Configuration](#configuration). Each folder setting already lists a container path after the `:`; it doesn't hurt in a native setup. Presets are set up on first run (see [Presets](#presets)); you can manage them later in the web UI.

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
| `HOST_MEDIA_DIR` | The folder that contains your shares (mounted read-only), e.g. `/Volumes/Media` | `/media` |
| `HOST_ENCODED_DIR` | Output folder | `/encoded` |
| `HOST_NOTE_DIR` | Optional: Obsidian folder | `/notes` |
| `WEB_PORT` | Optional: port on the Mac, default `7979` | `7979` |
| `TZ` | Optional: time zone of the container, e.g. `Europe/Zurich` (default `UTC`). Affects logs and stored times; the web UI always shows times in your browser's time zone. | – |

Shares inside `HOST_MEDIA_DIR` are available in the container as `/media/<folder>`. A share somewhere else (another volume) needs its own mount in a `docker-compose.override.yml` next to `docker-compose.yml` (Docker Compose reads it automatically; it's gitignored):

```yaml
services:
  encoding:
    volumes:
      - /Volumes/Archive/Shows:/media/Archive:ro
```

Then add the share under **Settings → Library** with the container path (here `/media/Archive`) after the `:`, and restart with `docker compose up -d`.

### 2. Start

```bash
docker compose up -d --build
```

On first start the container creates `config.env` in its data volume, with two example shares (`/media/Movies` and `/media/Shows`), and imports the bundled preset as the default. Adjust the shares to your folders under **Settings → Library**.

### 3. Open the web UI

Open <http://127.0.0.1:7979>. Under **Settings**, check that each folder has a green ✓. Then click **Scan now**.

### 4. Queue files

On the **Files** page, tick the files you want to encode, or tick a folder to select everything in it (e.g. a whole season), then click **Queue for encoding**. Any file can be queued this way, including files the scan didn't offer.

The same works on the command line, using the numbers from the last scan:

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
2. Make sure each folder setting in `config.env` contains the Mac path and the container path, e.g. `SHARE_1="/Volumes/Media/Movies:/media/Movies"`. The template already does this.
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
python3 encoding.py scan --share 3          # only share 3, e.g. after adding it
python3 encoding.py scan --share 3 --force  # share 3 again from scratch: re-read every file with ffprobe
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
| **Files** | All scanned videos as a folder tree with codec, resolution, bitrate, duration, size and status. Folders load their content when opened, and long lists show 100 subfolders (**Show more folders**) and 200 files (**Show more**) at a time, so the page stays fast with tens of thousands of files. Codecs are color coded: green for optimized codecs (H.265, AV1, H.266), orange for all others. Every folder shows how many of its files are already optimized, with a green/orange bar and a percentage badge (green when the whole folder is optimized). Totals and the optimized share at the top. Sort with the **Sort by** buttons at the top of the list (they stay visible while scrolling) or the column headers of an opened folder: file name, codec, resolution, bitrate, duration, size, status; each click cycles ascending, descending, off (default order by name); folders follow the sort for size (folder size) and status (optimized share), otherwise they stay sorted by name, and files without a value (not probed) come last. Opened folders open again after reloading the page. Filters: text search, status, and a codec filter that combines any of the codecs found with the groups "All optimized codecs" and "All other codecs". With a filter active, small matching folders open automatically. Select files or whole folders (they don't need to be opened) and click **Queue for encoding**; a folder queues all its files that match the active filters. The **↻** button next to every file and folder scans just that file or folder again (re-reading it with ffprobe), e.g. to check a file after replacing it. Files awaiting review have a **Replace…** button, see [Replacing originals](#replacing-originals). |
| **Status** | (Lists here and on other pages sort by clicking a column header: ascending → descending → off; the sort is remembered across pages and reloads.) Current encode with progress, fps and time left, and the queue with the state of every entry (waiting, encoding, done, failed, missing, already exists) and its preset. Progress and counts update live every 3 seconds without reloading; the list shows 100 entries at a time, can be filtered by state, and reloads when the next file starts or on **Refresh** — so it stays fast with thousands of queued files. Waiting entries can be ticked and removed (**Remove selected**, **Remove all waiting**); entries of the run in progress then show "Removed" and are skipped. Also the scan state, the automatic rescan schedule and the latest results; **Clear results history** forgets all results and deletes finished batch files, logs of failed encodes and replace results (encoded files are not touched). |
| **Stats** | Key figures (optimized share, size still to optimize, median bitrates) and charts of the distribution by codec (count and storage), bitrate, file size, resolution, library folder and status. Every chart has a table view, and hovering a bar shows its values. |
| **Settings** | The values from `config.env`, in separate sections (Library, Encoding, Obsidian, Worker). Each folder shows whether it exists. Changes apply immediately. |

There is **no login yet**. The web UI therefore listens on `127.0.0.1` only, and `docker-compose.yml` publishes the port only on `127.0.0.1`. Don't make it reachable from other machines until login is implemented. Values entered in the settings may use `$HOME`, but no other `$`, quotes or backslashes, because `config.env` is read by the shell.

## Configuration

### `config.env`

All machine-specific settings live in `config.env` in the data folder. It is created from `config.example.env` and stays out of git. You can edit it with a text editor or in the web UI.

| Key | Meaning |
|---|---|
| `SHARE_1`, `SHARE_2`, … | The shares (library folders) to scan, read only. See [Shares](#shares). |
| `SHARE_N_NAME` | Name of share N, shown everywhere. Defaults to the folder name. |
| `SHARE_N_PRESET` | Preset id for share N. Empty = `DEFAULT_PRESET`. |
| `DEFAULT_PRESET` | Preset id used for shares without their own. See [Presets](#presets). |
| `ENCODED_DIR` | Output folder for finished files, created if missing |
| `NOTE_DIR` | Obsidian folder for "Encoding To-Do.md" and "Encoding Status.md". Leave empty to skip notes. |
| `STATUS_NOTE_ENABLED` | `1` updates the status note every minute while encoding; `0` turns it off |
| `FFPROBE_WORKERS` | How many files ffprobe reads at the same time during scans. Empty (default) = number of CPUs. Lower it if the NAS struggles during scans. |
| `SCAN_INTERVAL` | Automatic rescan of all shares, in **minutes**: a scan starts once the last scan is that old. `0` (default) = off. See [Automatic rescan](#automatic-rescan). |
| `WORKER_ENABLED` | Docker only: `1` encodes in the container, `0` only runs web UI and scans |
| `PLEX_URL`, `PLEX_TOKEN` | Optional: Plex server address(es) and token, so Plex is refreshed after **Replace…**. See [Connecting Plex](#connecting-plex). |

Write values in double quotes and use `"$HOME"` rather than `~`. A folder setting can list several paths separated by `:`, and the first one that exists is used. That way one `config.env` works on the Mac and in the container.

### Automatic rescan

Set `SCAN_INTERVAL` to a number of minutes (in the web UI under **Settings → Scans**, or in `config.env`) to rescan all shares automatically, e.g. `SCAN_INTERVAL=30` for every 30 minutes. `0` turns it off, which is the default.

- The web UI checks once a minute whether the last scan (manual or automatic) is at least that old, and then starts a normal scan of all shares: only new or changed files are read with ffprobe. A rescan of an unchanged library mostly costs walking the folders (about 2–3 minutes for 70,000 files over NFS).
- It runs inside the web UI process, so it works whenever the web UI runs: always in Docker, and natively when the web UI's launchd agent is installed. Without the web UI, schedule `python3 encoding.py scan` yourself (e.g. a launchd job with `StartInterval` or a cron job).
- If a scan is already running, the automatic one waits for the next check. Changes to the interval apply within a minute, no restart needed.
- The **Status** page shows when the next automatic scan is due.

### Shares

The library consists of shares: folders that are scanned (read only), each with a name and a preset. There's no fixed split into movies and TV shows; use as many shares as your library has folders. Manage them in the web UI (**Settings → Library**) or in `config.env`:

```bash
SHARE_1="/Volumes/Media/Movies:/media/Movies"
SHARE_1_NAME="Movies"
SHARE_1_PRESET=""                                 # empty = DEFAULT_PRESET
SHARE_2="/Volumes/Media/Anime:/media/Anime"
SHARE_2_NAME="Anime"
SHARE_2_PRESET="anime-x265-10bit"                 # a preset id, see Presets
```

- Each share appears as its own top-level folder in the web UI and in the stats.
- **Scanning a single share:** under **Settings → Library**, a new share has an **Initial scan** button, and every scanned share has a **Full rescan** button that re-reads all its files with ffprobe (e.g. after files were replaced without their size or date changing). Both only touch that share; the other shares keep their data. **Scan now** on the Files and Status pages scans all shares, and only reads new or changed files with ffprobe. The Files page lists shares that haven't been scanned yet.
- The `:` separates fallback paths of the *same* folder (Mac path, container path), not different folders.
- **Keep each share's number.** It is part of how files are stored in the database (skipped files, results, the queue), so renaming a share is fine but renumbering isn't. To remove a share, comment its lines out (the web UI does this when you clear its folder); a commented-out number is never reused.
- All shares must be reachable for a scan and for the worker to start, so a disconnected share never makes files disappear from the list.
- Finished files from all shares land in the same output folder. Two files with the same name from different shares can't both be there: the second one is logged as `EXISTS`.

### Presets

HandBrake presets are managed on the web UI's **Presets** page:

- **Create** a preset from any of HandBrake's built-in presets, as a copy of an existing one, or by importing a file exported from the HandBrake app (Presets → Export; files with several presets or preset folders are imported completely).
- **Edit** the common settings in a form (container, video encoder and speed, quality as RF or bitrate, maximum resolution, frame rate, audio tracks/encoder/bitrate/mixdown, subtitles, chapters), or every setting in the full JSON. HandBrakeCLI checks a preset before it's saved.
- **Export** a preset as a HandBrake file, **make it the default**, or **delete** it (not while it's the default, assigned to a share, or used by queued files).

The choices in the editor and the list of built-in presets come from the installed HandBrakeCLI, so they always match its version: newer HandBrake versions bring their new encoders and presets along automatically. Without HandBrakeCLI (e.g. a web UI on a machine that doesn't encode), the editor offers a generic list and can't check presets.

Which preset a file gets:
1. the preset chosen when queueing (Files page: **Preset** next to **Queue for encoding**; CLI: `queue … --preset <id>`),
2. otherwise the share's preset (`SHARE_N_PRESET`),
3. otherwise `DEFAULT_PRESET`.

Presets are stored as HandBrake export files in `presets/` in the data folder, named by their id (e.g. `presets/1080p-x265-ger-eng-rf20.json`). The id is fixed when a preset is created, so renaming a preset doesn't affect shares or queued files. The output file's extension follows the preset's container (`.mkv`, `.mp4`, `.webm`).

### Replacing originals

Finished encodes wait in the output folder for your review. When you're happy with one, click **Replace…** next to the original on the **Files** page, or next to the finished entry on the **Status** page (in the queue and in "Latest results"; entries already replaced show "Replaced"):

1. Choose the file name. It's pre-filled with the encode's name (the original name; the extension follows the encoded container, e.g. `.mp4` → `.mkv`). Keeping the original name is best for Plex.
2. Choose whether to keep the original in `<output folder>/.replaced/` (default, same folder structure as in the share) or delete it.
3. **Move and replace**: the encode is moved into the original's folder under the chosen name, and the original is moved away or deleted. The page shows the result and checks the folder again.

The move is done by the worker (`encode.sh`), because the web UI may not be allowed to write to the library (in Docker it's mounted read-only): natively with launchd, or in the container if its library mount is writable. It also runs while an encode is in progress. It never overwrites another file, retries if the NAS briefly refuses the move, and puts the original back if the move fails.

**Plex:** with `PLEX_URL` and `PLEX_TOKEN` set (see below), the worker tells Plex right after the move: for the same file name it has Plex **analyze** that movie or episode (a library scan wouldn't notice a same-name replacement, and Plex would keep showing the old codec); for a new name or extension it has Plex **scan just that folder**. The result appears in the replace message. Without them, do it by hand: **Analyze** the item, or **Scan Library Files** with "Empty trash automatically after every scan" turned off, so the item keeps its watch history. Delete the backups in `.replaced/` once you're happy.

#### Connecting Plex

1. Get the token of your Plex server. On the Mac running Plex Media Server: `defaults read com.plexapp.plexmediaserver PlexOnlineToken`. Or in Plex Web: an item → ⋯ → Get Info → View XML, the address ends in `X-Plex-Token=…`. Treat it like a password.
2. Add to `config.env` (gitignored; the token isn't editable or shown in the web UI):
   ```bash
   PLEX_URL="http://127.0.0.1:32400"
   PLEX_TOKEN="…"
   ```
   In the mixed setup (Setup C), list both addresses, the Mac's and the one seen from the container: `PLEX_URL="http://127.0.0.1:32400 http://host.docker.internal:32400"`. The first that answers is used.
3. **Settings → Plex** shows whether the connection works and which Plex libraries match your shares.

The files must have the same paths for the worker as in your Plex libraries, which is the case when the worker runs on the Plex server's Mac with the same mounts.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ENCODING_DATA_DIR` | the program folder (Docker: `/data`) | Where `config.env`, `encoding.db`, `queue/`, `logs/` etc. live |
| `ENCODING_DB_FILE` | `$ENCODING_DATA_DIR/encoding.db` | Database file. It must be on a local disk, not on a network share. |
| `WEB_HOST`, `WEB_PORT` | `127.0.0.1`, `7979` (Docker: `0.0.0.0` inside the container) | Address of the web UI; also `webui.py --host/--port` |
| `WORKER_ENABLED` | not set | Docker only: overrides `WORKER_ENABLED` from `config.env` (set to `0` by `docker-compose.hybrid.yml`) |

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
| `replace/`, `replace/done/` | Replace jobs from the web UI and their results |
| `cancelled.txt` | Entries removed from the run in progress (skipped by the worker, deleted when the run ends) |
| `.handbrake-cache.json` | HandBrakeCLI's encoders and built-in presets, read again when HandBrakeCLI changes |

## Updating from version 2.x

Version 3 replaces the movie/TV show folders with shares and adds the preset store. The update is automatic: on the first run (any `encoding.py` command, or opening the web UI):

- `MOVIES_DIR`, `MOVIES_DIR_2`, …, `SHOWS_DIR`, `SHOWS_DIR_2`, … become `SHARE_1`, `SHARE_2`, … in that order, named after their folders. The old lines are commented out, and the previous file is kept as `config.env.v2.bak`.
- The preset from `PRESET_FILE`/`PRESET_NAME` is imported into the preset store and becomes `DEFAULT_PRESET`.
- All stored data moves to the new share keys: skipped files, results, the last numbered list, the file inventory (no new ffprobe pass needed), and batches still waiting in the queue.

Two things to do by hand: run `encoding.py` once (or open the web UI) before the native worker picks up a new batch (it waits and logs a hint until then), and in Docker replace `HOST_MOVIES_DIR`/`HOST_SHOWS_DIR` in `.env` with `HOST_MEDIA_DIR` (the folder that contains your shares), then adjust the container paths of the shares under **Settings → Library**.

## Updating from version 1.x

1. Let the queue run empty first (`queue/` and `processing/` contain no batch files). Batches from 1.x use the old German key prefixes, which the new worker no longer accepts.
2. Create `config.env` from `config.example.env` and set your library folders as shares (`SHARE_1`, …; they used to be in `encoding.py` and `encode.sh`).
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
| `Movies, Shows not reachable. Is the NAS mounted?` (share names) | Is the share mounted? Do the paths in `config.env` (or `.env` for Docker) match? |
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
