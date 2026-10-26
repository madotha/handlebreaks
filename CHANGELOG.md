# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Files page: the custom date range of the **Date added** filter uses date pickers (the browser's own, also on phones) instead of typed `dd.mm.yyyy` text. Future dates and a start after the end can't be picked. Older links with `dd.mm.yyyy` dates still work.
- Status page with three tabs and live counts: **Queue** (encoding, waiting, removed), **Awaiting review** (finished encodes waiting for Replace…, also from earlier runs) and **History** (every encode result and finished replace, newest first, filterable by result, paged). Every row can be ticked in every tab.
  - Awaiting review: **Replace selected…** next to Replace all finished…
  - History: **Queue again** for failed or stopped files (per row or selected; the file is queued with the preset it had).
  - **Dismiss selected**, **Dismiss all (shown)** and dismiss entries older than 1, 7 or 30 days hide entries on the Status page without deleting anything; a new result of the same file shows again, and the Files page's Failed marker stays.
  - Setting `HISTORY_KEEP_DAYS` (Settings → Status, default 30, 0 = always show) hides older history entries automatically.
  - Database: new table `dismissed` (created automatically; older versions ignore it).
- Files page: the file being encoded shows the status **Encoding** (it showed "Queued"), and an offered file whose last encode failed gets a red **Failed** marker next to its status (until a later encode succeeds or the results are cleared).

- Preset editor shows compatibility with the HandBrakeCLI that encodes: its version and platform, notes for the preset (made with a newer HandBrake, settings that HandBrakeCLI ignores, values it doesn't offer), markers on fields it doesn't know, and platform or hardware notes on options (VideoToolbox and Apple AAC "macOS only", Media Foundation "Windows only", NVENC, Quick Sync, AMD). In the mixed setup the Mac worker publishes its HandBrakeCLI's capabilities (`handbrake-worker.json`, at start, only when HandBrakeCLI changed), so the editor offers e.g. VideoToolbox encoders and Apple AAC and checks against the Mac's HandBrakeCLI instead of the container's older one. New command `encoding.py preset-check [--worker]` prints the compatibility report.

- Preset editor: fields for the settings of HandBrake 1.10 and later: color range, HDR dynamic metadata (HDR10+ / Dolby Vision), automatic audio track naming, and keeping the source's audio and subtitle track names. Old settings HandBrake still exports (`PictureAutoCrop`, `x264Option`, `x264UseAdvancedOptions`) are labeled as such under More.

- The name suggested by **Replace…** ends with the encode's quality after the release group: `x265-donm.rf20` (constant quality) or `x265-donm.avg2000` (average bitrate in kbit/s). `encode.sh` records the preset and quality of every finished encode in `logs/encodes.tsv`; for older encodes the preset is looked up (queued with, share's, default). Setting `RENAME_QUALITY` (Settings → Replace, on by default).
- Settings → Library: the shares as a compact list (name, folders with ✓/✗, preset, files, last scan), expandable to edit them, with icon buttons to edit, scan (initial or full rescan) and remove a share, and **+** to add one. Removing comments the share out, so its number stays reserved; the last share can't be removed.

- Preset editor with every HandBrake setting, in the tabs of the HandBrake app: Summary, Dimensions (crop, size, anamorphic, rotation, borders), Filters (detelecine, interlace detection, deinterlace, denoise, chroma smooth, sharpen, deblock, color space, grayscale), Video (also tune, profile, level, multi-pass, frame rate mode, extra encoder options, hardware decoding, scaler), Audio (passthru, fallback, any number of tracks with all their settings) and Subtitles; other settings under More. Choices (encoders and their speeds, tunes, profiles and levels, filter presets and tunes, sample rates, dither) come from the installed HandBrakeCLI and follow each other (e.g. the tunes of the chosen encoder). Saving an unchanged form changes nothing.

- Docker: `WEB_BIND` in `.env` sets who can reach the web UI (default `127.0.0.1` = this machine only; `0.0.0.0` = the whole network, without a login).

- Web UI on phones: bottom tab bar, compact two-line file rows (tap a name for bitrate, duration and date added), stacked queue and preset lists, settings sections as scrollable chips, dialogs as bottom sheets, no horizontal scrolling.
- Theme toggle in the header: system, light or dark, remembered by the browser and applied before the first paint. A favicon (the logo mark).

- Files page: **Date added** filter (last 24 hours, last 7 days, last 30 days, or a custom range with dd.mm.yyyy dates; the end date is included) and an **Added** column (dd.mm.yyyy hh:mm in the browser's time zone), also as a sort field (folders by their newest file). "Date added" is when a scan first found the file: new column `files.added`, filled once from the file's modification date for files already in the database, and from the file date on a share's first scan. After **Replace…** the new file keeps the original's date (files replaced before this version show their replace date).

- Bulk replace: **Replace selected…** on the Files page (ticked files and folders) and **Replace all finished…** on the Status page open a list of every file with a finished encode, each with its suggested name to edit, and replace them in one go. All names are checked first (nothing starts if one is invalid or two collide). The worker moves all files of a run first and then refreshes Plex once per folder instead of once per file.

- **Replace…** suggests a new file name: the original name with its codec tag (`h264`, `x264`, `H.264`, `AVC`, `XviD` …) changed to the encode's codec, read with ffprobe (`…h264-NTB.mkv` → `…x265-NTB.mkv`); names without a codec tag get it before the release group. New settings (Settings → Replace): `RENAME_CODEC_TAGS` (tag per codec, default `hevc=x265 av1=AV1 vvc=VVC h264=x264`) and `RENAME_GROUP` (optional own release group replacing the original one). The name can still be edited before replacing.

- Stop the running encode from the Status page: **Stop, continue with next** ends the current file (the partial file is deleted, the result is `STOPPED`) and goes on with the queue; **Stop all** also puts the rest of the run back into the queue and pauses it until **Resume** (replace jobs keep running while paused). The worker checks for a stop request (`stop.txt`) every 5 seconds; the pause is the file `paused` in the data folder. A stopped file is offered again and can be queued later. The buttons only appear when the running worker supports stopping (it marks its lock with `.lock/stoppable`); a worker started by an older version shows a note instead, until its run is over.

- **Collapse all** on the Files page (in the sticky sort bar): closes every open folder, forgets them for reloads and unloads their content; folders with ticked files inside stay loaded, so the selection is kept.

- Housekeeping on the Status page: tick waiting queue entries and **Remove selected**, or **Remove all waiting**. Batches in `queue/` are rewritten; entries of the run in progress go to `cancelled.txt` (`batch<TAB>key`), show as "Removed" and are skipped by `encode.sh`, which deletes the file when the run ends. Removed files are offered again on the Files page. Also `encoding.remove_queued()`.
- **Clear results history** on the Status page (with confirmation): forgets all encode results (database and `logs/results.tsv`) and deletes finished batch files (`done/`), logs of failed encodes and results of replace jobs. Also `encoding.clear_results()`.
- `FFPROBE_WORKERS` (Settings → Scans): number of parallel ffprobe calls during scans; empty = number of CPUs (before: fixed 4).
- The sort of the Files page and the Status queue is remembered (per browser session) when switching pages or reloading; "off" resets it. Tables sorted in the browser remember their sort per page.
- Folders on the Files page only open when clicked: every page load starts with all folders collapsed, also with a filter active (small matching folders no longer open by themselves). Folders you open again load instantly: the browser keeps loaded folders for its tab as long as the library data and queue are unchanged.

- Sorting by column header everywhere: a click cycles ascending → descending → off (default order). Files page: file name, codec, resolution, bitrate, duration, size, status, via a "Sort by" bar that's always visible at the top of the list (part of the sticky queue bar) and via the column headers of opened folders. Status queue: file, preset, state (off = processing order). Latest results, the Presets list and the Stats table views sort in the browser (`static/tablesort.js`, numbers numerically, empty values last). Sorting happens on the server, so all pages of a folder follow it; folders are ordered by size or optimized share when sorting by size or status, otherwise by name; files without a value come last; ties are ordered by name.
- Plex integration: with `PLEX_URL` and `PLEX_TOKEN` in `config.env`, the worker tells Plex about a replaced file right away: same file name → the movie or episode is analyzed (found by its path through Plex's folder view); new name or extension → Plex scans just that folder. Several URLs can be listed (e.g. for the Mac and the Docker container). Settings → Plex tests the connection; the token is never shown. A Plex problem never blocks a replace, it's only reported.
- **Replace…** also on the Status page: next to finished entries in the queue and in "Latest results" while their encode is in the output folder; replaced entries show "Replaced". The dialog and its logic are shared (`templates/_replace_dialog.html`, `static/replace.js`).
- **Replace…** button for files awaiting review (Files page): opens a dialog to choose the new file name (pre-filled; the extension follows the encoded container) and whether to keep the original (in `<output>/.replaced/`, default) or delete it. The web UI writes a replace job (`replace/`); the worker (`encode.sh`, natively or in the container) carries it out, also while an encode runs, retries moves the NAS briefly refuses, never overwrites another file, and puts the original back if the move fails. The page waits for the result and checks the folder again. Also `encoding.py replace-jobs`.
- ↻ button on every file and folder of the Files page: re-reads just that file or folder with ffprobe (new files are added, removed ones dropped) and shows the result right away without leaving the page; opened folders stay open. Also `scan(path=…)` in `encoding.py`. Such checks don't reset the automatic rescan timer.
- Automatic rescan: `SCAN_INTERVAL` (minutes, default `0` = off; Settings → Scans) rescans all shares once the last scan is that old. It runs in the web UI process, and the Status page shows when the next one is due.
- Live queue on the Status page: every queued entry with its state (waiting, encoding, done, failed, missing, already exists) and preset. Progress and counts update every 3 seconds without reloading the page, the list is paged (100 entries), filterable by state, and reloads when the next file starts or on "Refresh", so it stays fast with thousands of queued files.
- launchd template `local.plex-encoding.plist` also watches `replace/`; reinstall the agent (README, Setup A step 7) to start replace jobs immediately.
- Docker: `TZ` in `.env` sets the container's time zone (default UTC); the image now includes tzdata.
- Folder status in the Files page tree: every folder and share shows how many of its files are already optimized ("36 of 48 optimized"), a bar split into optimized (green), other codecs (orange) and not probed (gray), and a percentage badge that is green only when the whole folder is optimized. The counts follow the active filters.

### Changed

- Status page: finished encodes and results no longer stay in the queue list. They are in Awaiting review and History, where they can be selected, dismissed and (for failed or stopped files) queued again. The "Latest results" list (last 20) is replaced by History. **Clear results history** is now **Delete history data…** in the History tab; it also forgets dismissals.
- Icon buttons with tooltips for frequent row actions: scan again (round arrow) and **Replace…** (arrows) on the Files and Status pages, reload the queue, collapse all folders, and edit, export, make default and delete on the Presets page. Hovering (or keyboard focus) shows what a button does right away. Primary and destructive actions keep their text.
- Status page: finished encodes stay in the Queue section (as "Done", with **Replace…**) until they're replaced, also after the worker's run is over. **Replace all finished…** moved into the Queue section.
- The file name fields of **Replace…** and bulk replace grow with the name (wrapping over several lines), so the whole name is always visible; Enter still starts the replace, and pasted line breaks are removed. The replace dialog is wider.
- Redesigned web UI, Plex-inspired: dark-first neutral grays with Plex amber reserved for actions (buttons, active tab, links, focus), a sticky translucent header with icons, quieter borders and clearer hierarchy (tiles, badges, progress, the current encode with its percentage up front), consistent control sizes, and long file names that wrap at their dots and dashes instead of in the middle of a word.
- Files page: folders with many subfolders show 100 at a time (**Show more folders**) and the markup is compacted. Opening a share with 1,100 shows went from ~1 s to ~0.15 s in the browser, and a reload with all shares open from ~3.5 s to ~0.6 s.
- Faster web UI and scans: the Files tree and Stats are aggregated in SQL (Stats ~2× faster) with a covering index, library totals and the unfiltered top level are cached until the database changes, HTML and JSON responses are gzip-compressed (a 1.4 MB folder now transfers as ~60 KB, with slimmer markup), new lines of `results.tsv` are imported incrementally, HandBrakeCLI's capabilities and built-in presets are cached on disk (`.handbrake-cache.json`), and ffprobe reads less of each file (~30 % faster per file, same results).
- Times in the web UI are shown in the viewer's time zone (as `<time>` elements converted in the browser). Before, they appeared in the server's time zone, which is UTC in a Docker container by default.

### Fixed

- A file that already finished (or failed) in the worker's current run can be queued again right away. It was refused as "already in the queue" until the run was over, because entries of finished files stay in the run's batch.
- A scan saves its ffprobe results every 200 files. An interrupted scan (restart, closed terminal, unreachable NAS) keeps the probes done so far, and the next scan resumes with the remaining files instead of starting over. Files probed by an unfinished scan have no status until a scan completes.
- Runs with several batches: once the worker had finished the first batch (and moved it to `done/`), the Status page numbered the remaining entries wrongly and showed files as "Done" that hadn't been encoded yet; they couldn't be removed either. **Stop all** in such a run put no files back into the queue and silently dropped the rest of the run. The worker now lists its run's batches in `logs/run.txt`, and the Status page and **Stop all** read the run from it. Entries without a result from the current run show as "Skipped" instead of "Done"; results from earlier runs no longer count.
- **Replace all finished…** found nothing until the next scan: it looked for files marked "awaiting review", but files encoded since the last scan are still marked "queued" in the database. A file now counts as awaiting review as soon as its encode is in the output folder, also on the Files page (status label and **Replace…** button).
- Elements hidden with the `hidden` attribute but styled as flex rows stayed visible: the Status page always showed the stop buttons and the "Queue paused" bar with **Resume**, even with nothing encoding or paused.
- Replaces seemed cancelled when the page was reloaded before they finished: the files were replaced, but the list kept showing the old file, because the page itself had to trigger the folder rescan afterwards. The web UI now rescans the folder of every finished replace job on its own (background check every 10 s, and when a page asks for the result), and files with a pending replace show **Replacing…** instead of the button after a reload, so a job can't be sent twice.
- Encodes on a network share were marked as failed although they succeeded: when launchd starts `encode.sh` on macOS, `/bin/mv` and `/bin/rm` get "Operation not permitted" on the NFS share (Python's file functions are allowed). Moving the finished file out of `.inprogress/` and removing partial files now go through Python (`fs` helper in `encode.sh`).
- `encode.sh` reported a file as done even if moving it out of `.inprogress/` failed (seen on a Synology NAS while its media indexer read the new file: "Operation not permitted"). The move is now retried up to 6 times, 10 s apart; if it still fails, the file is marked as failed and the encode stays in `.inprogress/`.
- Preset check on macOS: presets with Apple's AAC encoder (`ca_aac`) were rejected by a native HandBrakeCLI, because the check always replaced `ca_aac` with `av_aac` (which only exists in other builds). The replacement now only applies where `ca_aac` itself isn't available.

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
