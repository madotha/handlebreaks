# Changelog

All notable changes to Handlebreaks are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.20.1] - 2026-10-30

### Fixed
- A worker stopped right after an encode finished could leave the finished file in the output folder's `.inprogress/` without a result, and encode it again on the next start. The stop now waits until the file is moved and its result logged.

## [3.20.0] - 2026-10-30

### Changed
- Stopping the worker no longer loses the encode in progress, and no longer waits for Docker to kill it. On `docker compose stop`/`restart`, an update with `docker compose up -d --build`, logging out of the Mac, `launchctl unload` or Ctrl+C, the worker ends HandBrakeCLI, deletes the partly encoded file and puts it and the rest of the run back into the queue (not marked as stopped or failed, not paused). A replace being carried out finishes first. The web UI shows a "Run interrupted" notification that says the worker was stopped. The container gets up to 2 minutes for this (`stop_grace_period`).
- Docker: the container is hardened: read-only system (only the volumes and `/tmp` are writable), all Linux capabilities dropped, `no-new-privileges`.

### Fixed
- A stop signal only removed the worker's lock, and the worker kept encoding until it was killed. In Docker, stopping the container took 10 seconds and cut the encode off; a second worker could even start meanwhile.
- Docker: a replace job left half-done by a stopped container only went on once something else was queued.

## [3.19.0] - 2026-10-30

### Added
- Status page: a search box in every tab (words of the file name or folder; the page address keeps it). **Dismiss all shown** in Awaiting review and History applies to what the search shows.
- Status page: after ticking all entries shown, **Select all N matching** selects every entry of the tab (with its filter and search), not only the 100 loaded. All buttons and dialogs (Queue again, Dismiss, Delete selected…, Replace selected…, Move, Remove) then act on all of them.
- Status → Queue, Awaiting review and History: **Codec** and **Resolution** columns (History: hidden until shown in the Columns menu), and Bitrate in the Queue tab, to help choose a preset. Kept originals: the **Preset** and **Bitrate** of the file that replaced each original.
- Files page: the bin (delete the encode, optionally encode again with another preset) and **×** (cancel a replace that hasn't started) next to files with a finished encode, like on the Status page. Their bitrate shows the encode's below the original's.
- Files page: adjustable column widths and the **Columns** menu, one setting for all folders.
- Files page: folders show how many of their files are offered, queued, waiting for review and failed (e.g. "42 offered · 1 queued · 2 to review").

### Changed
- Status and Files tables: the table header stays visible while scrolling long lists.
- Files page: buttons have their own column instead of sharing the Status column, which now only shows the status. A file with a finished encode shows "Awaiting review".
- Resolution shows as a class (4K, 1080p, 720p, SD …); the exact size appears on hover.
- Column minimum widths only limit dragging; the automatic layout sizes columns by their content again.

### Fixed
- Status → History: with failed entries shown, ticking all of them didn't mark the "select all" box as fully ticked.

## [3.18.0] - 2026-10-30

### Added
- Status page: column widths can be adjusted by dragging the edge of a column header (or with ←/→ on the focused edge). Each column keeps a minimum width so its content stays readable, and the widths are remembered per tab in the browser. Double-click an edge, or use **Reset widths**, to go back to automatic widths.
- Status page: the **Columns** menu (at the right end of the table header; on phones next to **Select all shown**) hides the columns you don't need, per tab. The file name always stays.

### Changed
- Status → Awaiting review and History: the bitrate shows on two lines, the original's first and the encode's below it. Saved shows the amount and the percentage on two lines. Dates, presets and other values wrap between whole values, never inside a number, when a column gets narrow.

## [3.17.0] - 2026-10-30

### Added
- Status → Awaiting review and History: a sortable **Bitrate** column, to help choose a preset. It shows the original's overall bitrate from the last scan, followed by the encode's (`11.1 Mbit/s → 4.0 Mbit/s`) while the encode is still in the output folder. The encode's bitrate is estimated from its size and the original's duration. For replaced files, History shows the bitrate of the file that replaced the original.

## [3.16.0] - 2026-10-30

### Added
- Cancel replaces that haven't started yet: **Cancel** next to each waiting file in the replace progress bar's details, **Cancel waiting** for all of them, and **×** next to "Replacing…" in Status → Awaiting review. Until the worker starts moving the files nothing is touched, so a cancelled replace leaves the original and the encode where they are. Once the worker has started, the replace can't be cancelled any more.
- Status → Awaiting review: delete finished encodes you aren't happy with (bin icon per row, or **Delete selected…**). The dialog can also put the files back into the queue with another preset (**Delete and encode again**). Files with a replace in progress or already in the queue are left out. The original in the library is never touched.
- Status → Awaiting review: a **Preset** column showing which preset each encode was made with.

### Changed
- The worker now claims each replace job before it moves any file: it moves the job from `replace/` into the new folder `replace-running/`. A job left there by a worker that stopped mid-way is carried out on the worker's next start. Update the worker together with the web UI (in the hybrid setup, `git pull` the native install as well as rebuilding the container); an older worker doesn't claim jobs, so with it a cancel that comes while the files are already being moved first shows as cancelled and then as replaced.

### Fixed
- The replace progress bar stopped updating (its request failed) once a replace was finished in setups without Plex.

## [3.15.0] - 2026-10-29

### Added
- Notifications: when more arrive than fit on screen (4, 3 on phones), the rest wait behind a **+N more** chip instead of the oldest disappearing unread. They move up as others go, and their time only starts once they show. The chip shows all of them and then turns into **Close all**.

### Changed
- Status page: the open tab's list reloads as soon as its count changes, e.g. when an encode finishes (Awaiting review, History) or entries are queued elsewhere. Before, it waited for the next file to start or for **Refresh**, so the last file of a run only showed up after a manual refresh.

## [3.14.0] - 2026-10-29

### Added
- Status → Kept originals: the line above the list shows how much space replacing them saved (recorded for replaces since 3.13; older ones are counted as "recorded for N of them").
- Status → Kept originals: **Also when the replacement is missing**, to delete originals that are otherwise kept (see below).

### Changed
- Kept originals whose replacing file is no longer in the library, or whose share can't be reached to check it, are no longer deleted: not by **Delete selected…**, not by **Delete…** older than N days, and not automatically (`KEEP_ORIGINALS_DAYS`, `purge-originals`). Before, they were only marked with a warning. Tick them and **Also when the replacement is missing** to delete them anyway.
- An unmounted share (an empty mount point) now shows as "library not reachable" in Kept originals instead of "replacement missing".

### Fixed
- A worker run that ends while the web UI isn't running (e.g. during an update or container rebuild) still gets its "Run finished" notification once the web UI is back.

## [3.13.1] - 2026-10-29

### Fixed
- Status page on phones: a **Select all shown** checkbox above the list. The table header with its select-all box is hidden on phones, so there was no way to tick all entries at once.
- Safari: closing a Replace dialog puts the keyboard focus back on the button that opened it (other browsers already did), instead of starting over at the top of the page.
- Safari: the HandBrake compatibility box in the preset editor no longer shows a stray ▼ marker.

## [3.13.0] - 2026-10-29

### Added
- A fun fact about your library at the top of the Stats page, a different one on every visit: how long it would take to watch everything, how many DVDs or floppy disks it would fill (and how high that stack would be), how much encoding the rest could free, the biggest, most frugal and highest-bitrate files, the oldest title, what you added in the last 30 days, and more. Facts without data (e.g. an unprobed library) are skipped; runtimes over 6 hours and bitrates over 150 Mbit/s count as probe glitches.
- Replace results record the size of the original and the replacement, so the space Handlebreaks freed can be counted (from this version on).

## [3.12.0] - 2026-10-29

### Added
- Status → Awaiting review has a **Saved** column with the space replacing each original frees (and its percentage; "larger" if the encode is bigger), sortable like the other columns, and a line above the list with the total: the originals' size, the encodes' size and how much replacing them all would save. It updates with the list.

## [3.11.1] - 2026-10-29

### Fixed
- The Status page refreshes its lists whenever a replace moves on (moved, checked by Plex, failed) and when all are done, so History shows **Plex ✓** once Plex confirmed the file, without a reload. Before, the page reloaded as soon as the file was moved (before Plex's check) and then kept showing **Plex …** until the next encode started or Refresh was clicked. Finished replaces no longer reload the whole page.

## [3.11.0] - 2026-10-29

### Added
- A compact progress bar at the top of the Status and Files pages while replaces are ongoing: one line with "Replacing 4 of 8", a bar in parts (done, moved and being checked in Plex, failed, waiting for the worker) and the Plex count, plus **Details** for a short list of the files. It includes every ongoing replace, also ones started on another page or tab, grows when more are started, survives reloads, and shows a summary for a few seconds when all are done.

### Changed
- The Replace dialogs no longer show the blue "Replacing … waiting for the worker" text; the progress bar shows it. Errors are still shown below the toolbar.

## [3.10.0] - 2026-10-29

### Added
- After a replace, the web UI checks that Plex picked up the new file (same path and size, and no longer the old file) and scans the folder once more if not. Status → History shows the result next to each replace (Plex ✓, … or ✗), and a replace Plex didn't pick up becomes a "Plex not updated" notification.
- `PLEX_PATH_MAP` (Settings → Plex): for Plex servers that see the library under other paths than the worker, e.g. Plex on the NAS (`worker path=Plex path`, like Sonarr's and Radarr's path mapping). Before, Plex could only be notified with identical paths.

### Changed
- Replaces in the same folder share one Plex scan even when they finish in different worker rounds: a folder is scanned once no new replace came for 15 seconds (at most 60 seconds after the first). Plex is now told by the web UI, not by the worker; in Setup C update the native installation and the container together.
- A file replaced under the same name is analyzed and its folder scanned, since a scan alone may miss it.
- Replacing puts the new file into the folder before the original leaves it (under a new name), or swaps it in with one rename (under the same name, keeping the original by a hard link where possible), so Plex never sees the item without a file.
- Kept originals whose replacement Plex didn't confirm are not deleted automatically (`KEEP_ORIGINALS_DAYS`), and Status → Kept originals marks them "Plex not updated".

## [3.9.0] - 2026-10-28

### Changed
- The worker's plist (Setup A and C) starts the worker at login and whenever a batch was left in `processing/`, e.g. after a crash or restart. Install it again to get this, see [Updating to version 3.9](https://github.com/madotha/handlebreaks/wiki/Updating#updating-to-version-39); without it everything works as before.
- The results of replace jobs are kept in `replace-done/` instead of `replace/done/` (moved automatically at the first start).
- The worker exits right away when there's nothing to do, without checking the library.

### Fixed
- launchd no longer starts the worker every 30 seconds while nothing is queued: the permanent `replace/done/` folder kept `replace/` from ever being empty. Leftover batches in `processing/` were only picked up because of that.

## [3.8.0] - 2026-10-28

### Added
- The web UI shows a banner when the worker can't start encoding, even if the web UI itself sees the library fine (e.g. in Docker while the Mac encodes): "The worker can't read …" when access is refused, with the fix for macOS (Full Disk Access for `/bin/zsh`), "The worker can't reach …" when the share isn't mounted on the machine that encodes, and "The worker can't find HandBrakeCLI". The worker writes the reason to `logs/worker-wait.txt` while it waits.

### Changed
- The worker tells a refused access apart from an unmounted share: it logs `Library not readable (…): Operation not permitted` (or `Permission denied`) with a hint instead of `Library not reachable`.

### Fixed
- Files that ffprobe couldn't read are read again on the next scan, instead of keeping the error until the file changes. Broken files are tried again on every scan (cheap, there are usually few).

## [3.7.0] - 2026-10-28

### Changed
- The tool is called **Handlebreaks** everywhere: in the web UI (header, page titles, app name when added to a phone's home screen), the README, the wiki, the Obsidian notes, the configuration templates and `encoding.py version` (prints `Handlebreaks vX.Y.Z`).
- Docker: the image and the container are named `handlebreaks` (before: `plex-encoding-automation` and `<folder>-encoding-1`). `docker compose` commands are unchanged; `CONTAINER_NAME` in `.env` sets another name, e.g. for a second instance. See [Updating to version 3.7](https://github.com/madotha/handlebreaks/wiki/Updating#updating-to-version-37) in the wiki, also for renaming an older install folder.
- New installs clone into a folder named `handlebreaks`.
- Requests to Plex identify themselves as Handlebreaks (`X-Plex-Product`).

### Added
- The Settings page shows the version with links to the documentation and the changelog.

## [3.6.2] - 2026-10-28

### Changed
- Notifications on screen stay when you switch pages or reload (in the same tab): they come back where they were, timed ones with the time they had left, errors until you close them.

## [3.6.1] - 2026-10-28

### Changed
- Notifications show a bar at the bottom that runs down until they hide. Hovering the notifications now holds all of them, so none disappears or moves away under the mouse.

## [3.6.0] - 2026-10-28

### Added
- Notifications in the web UI: popups in the bottom right corner of every open page (above the tab bar on phones) when an encode finished or failed, a worker run finished or was interrupted by an offline share, a replace was done or failed, a scan finished or failed, a share went offline or came back, or kept originals were deleted automatically. Each links to where it happened and has a close button; several encodes or replaces at once become one notification.
- Settings → Notifications: `NOTIFY_SECONDS` (how long a notification stays, default 8 seconds, 0 = until closed), `NOTIFY_ERRORS_STAY` (errors stay until closed, on by default) and `NOTIFY_EVENTS` (which kinds to show, all by default).

## [3.5.1] - 2026-10-28

### Changed
- Status page: every entry (and the file being encoded) shows its share and folder as a small line above the file name, instead of one line with the name right after the path. Rows stay as compact as before.

## [3.5.0] - 2026-10-28

### Added
- `DATE_FORMAT` setting (Settings → Display): show dates in the web UI as `YYYY-MM-DD` (default), `DD-MM-YYYY`, `MM-DD-YYYY`, `DD.MM.YYYY`, `DD/MM/YYYY` or `MM/DD/YYYY`.
- `KEEP_ORIGINALS` setting (Settings → Replace): whether "Keep the original" is ticked by default in the Replace dialogs. On by default, as before.
- Status → Awaiting review shows the size of each encode (sortable), with the original's size and the difference in its tooltip.

### Changed
- The "Added" date on the Files page follows `DATE_FORMAT` like every other date; it was always shown as `DD.MM.YYYY` before.

### Fixed
- The Replace dialog no longer names a specific output folder; it says "`.replaced/` in the output folder" like the bulk dialog.

## [3.4.3] - 2026-10-28

### Added
- The web UI shows a "Not reachable" banner while a share or the output folder is offline (in Docker with a hint to restart the container if the share was mounted after it started). Automatic scans wait until every share is back.
- README: mounting NFS shares with autofs so they're back right after a restart, and a section on restarts and offline shares.

### Fixed
- When a share or the output folder goes offline during a run (e.g. unmounted before a restart), the worker no longer marks the rest of the run as missing or the current encode as failed. It puts the current file and the rest of the run back into the queue and retries until the share is reachable again.
- A share whose folder is empty now counts as unreachable for the worker and for scans, because an unmounted share usually leaves an empty mount point behind. Before, a scan could have treated it as an empty share.

## [3.4.2] - 2026-10-28

### Fixed
- The worker no longer deletes a finished encode when the shell reports a wrong non-zero exit code (seen as 19) for a HandBrakeCLI run that succeeded. It now also checks HandBrake's own log (`work result = 0`, `Encode done!`) and a non-empty output file, and keeps the file if all agree. Real failures and stops are unchanged.

## [3.4.1] - 2026-10-27

### Fixed

- Status → Kept originals no longer lists files that aren't originals, such as the `SYNOINDEX_MEDIA_INFO` index data a Synology NAS puts into `@eaDir` folders: only video files are listed, and `@eaDir` is skipped. Deleting a kept original now also removes the NAS's index data of that file and the folders left empty.

## [3.4.0] - 2026-10-27

### Added

- Status page, new tab **Kept originals**: lists the originals that Replace… kept in `<output folder>/.replaced` with their size, when they were kept and how much space the replacing encode saved, with a warning if the file that replaced an original is no longer in the library. Delete selected originals, or all kept longer than 7, 30, 90, 180 or 365 days, permanently (always after a confirmation with the number of files and the space).
- `KEEP_ORIGINALS_DAYS` (Settings → Replace, default 0 = never): deletes kept originals automatically once they were kept that long, checked hourly while the web UI runs. `python3 encoding.py purge-originals [days]` does the same on demand.

## [3.3.0] - 2026-10-27

### Added

- Preset editor: every setting has an ⓘ button with a description of what it does (tooltip on hover or focus, expands below the field on click and on phones), and dropdowns explain the chosen option below the box (encoders, speed presets, tunes, profiles, levels, filters, deinterlacing, mixdowns, sample rates, …). The texts are in `preset_tips.py`.
- Preset editor: the tabs are now a proper tab bar with icons that stays visible while scrolling, the long tabs are split into sections (Crop, Size, Encoder, Quality, …), and a dot marks tabs with unsaved changes.
- `encoding.py preset-check` lists editor fields without a description.

## [3.2.0] - 2026-10-27

### Added

- Status page, Queue tab: change the order of waiting entries. Drag a row by its handle (mouse; a ticked row takes all ticked rows along), use the up/down arrows of a row (also on phones), or tick rows and click **Move to top** / **Move to bottom**. A divider marks where the next run starts: entries of the worker's current run stay put until it takes them, while moving one of them puts it and everything after it into a new batch that a new worker run starts once the current run has finished (the old places show as "Removed"). Presets chosen when queueing are kept. Reordering is off while a column sort is active.
- Selecting several rows at once on the Files and Status pages: **Shift+click** ticks (or unticks) everything between the last clicked row and this one, and **dragging** over the checkboxes of several rows ticks them all (unticks them if the first row was ticked). The folder checkboxes on the Files page work the same way among the folders of one level. Mouse only; on touch screens dragging still scrolls.

## [3.1.0] - 2026-10-26

### Added

- Status page: the lists show "Showing 100 of 250" with **Show more** when they have more than 100 entries (History, Awaiting review, Queue), keeping the filter and sort. On phones, where the table header is hidden, "Sort by" chips sort the lists.
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

- HandBrake compatibility checked against HandBrake 1.11.2 (the newest release, 7 June 2026; preset format 72): `encoding.py preset-check` and `preset-check --worker` report every setting as covered by the preset editor. The Docker image still has Debian's HandBrakeCLI 1.9.2.
- Status page: finished encodes and results no longer stay in the queue list. They are in Awaiting review and History, where they can be selected, dismissed and (for failed or stopped files) queued again. The "Latest results" list (last 20) is replaced by History. **Clear results history** is now **Delete history data…** in the History tab; it also forgets dismissals.
- Icon buttons with tooltips for frequent row actions: scan again (round arrow) and **Replace…** (arrows) on the Files and Status pages, reload the queue, collapse all folders, and edit, export, make default and delete on the Presets page. Hovering (or keyboard focus) shows what a button does right away. Primary and destructive actions keep their text.
- Status page: finished encodes stay in the Queue section (as "Done", with **Replace…**) until they're replaced, also after the worker's run is over. **Replace all finished…** moved into the Queue section.
- The file name fields of **Replace…** and bulk replace grow with the name (wrapping over several lines), so the whole name is always visible; Enter still starts the replace, and pasted line breaks are removed. The replace dialog is wider.
- Redesigned web UI, Plex-inspired: dark-first neutral grays with Plex amber reserved for actions (buttons, active tab, links, focus), a sticky translucent header with icons, quieter borders and clearer hierarchy (tiles, badges, progress, the current encode with its percentage up front), consistent control sizes, and long file names that wrap at their dots and dashes instead of in the middle of a word.
- Files page: folders with many subfolders show 100 at a time (**Show more folders**) and the markup is compacted. Opening a share with 1,100 shows went from ~1 s to ~0.15 s in the browser, and a reload with all shares open from ~3.5 s to ~0.6 s.
- Faster web UI and scans: the Files tree and Stats are aggregated in SQL (Stats ~2× faster) with a covering index, library totals and the unfiltered top level are cached until the database changes, HTML and JSON responses are gzip-compressed (a 1.4 MB folder now transfers as ~60 KB, with slimmer markup), new lines of `results.tsv` are imported incrementally, HandBrakeCLI's capabilities and built-in presets are cached on disk (`.handbrake-cache.json`), and ffprobe reads less of each file (~30 % faster per file, same results).
- Times in the web UI are shown in the viewer's time zone (as `<time>` elements converted in the browser). Before, they appeared in the server's time zone, which is UTC in a Docker container by default.

### Fixed

- Settings: for an older `config.env` without `HISTORY_KEEP_DAYS` or `RENAME_QUALITY`, the form showed an empty field or an unticked box, so saving the section turned the defaults (30 days, quality in suggested names on) into 0 / off. The form now shows the effective defaults.
- Status page: the box with the three tabs no longer shows a scrollbar (the tab underline made it 1 px too tall), and on phones the tabs fit even with large counts ("Awaiting review" is shortened to "Review").
- Phones: icon buttons no longer shrink to a sliver in a row (Edit and Export on the Presets page), long paths in the share cards of Settings → Library wrap next to the action icons instead of running under them, and the logo, collapsible section headers and preset names have larger touch targets.
- Settings → Library: the "Output" heading was shown twice.
- Files that finished or failed in the worker's current run no longer count as queued until the run ends. A file that just failed shows as offered with its red **Failed** marker right after the next scan (it stayed "Queued"), its checkbox on the Files page is no longer locked as "Already queued", and the status note and `encoding.py status` count only waiting and encoding files (finished ones were included).
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
