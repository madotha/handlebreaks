# Plex Encoding Automation (H.264 → H.265)

Semi-automatic conversion of new movies and TV episodes from H.264 to H.265 (x265) with HandBrakeCLI on a Mac. Before anything is encoded, you choose which files to convert.

> Command-line output, code comments and the generated Obsidian note are in German.

## Components

| File | Purpose |
|---|---|
| `encoding.py` | Scans the library, applies filters, tracks state, writes the Obsidian note, and turns your selection into a queue job |
| `encode.sh` | Worker: processes `queue/` with HandBrakeCLI and `preset.json` |
| `local.plex-encoding.plist` | launchd agent (`QueueDirectories`) that starts `encode.sh` as soon as a job appears in `queue/` |
| `status.sh` | Live view in the terminal (progress, fps, time remaining, pending files) |
| `config.example.env` | Template for `config.env`, the local settings file (paths, preset, notes) |
| `preset.json` | Preset exported from the HandBrake app (x265, RF 22, MKV) |

## Requirements

- **macOS.** The worker relies on launchd, `caffeinate` and zsh. The preset uses Apple's AAC encoder (`ca_aac`), which only exists in HandBrake on macOS.
- **Python 3.8 or later.** Only the standard library is used. The Xcode Command Line Tools or Homebrew both provide `python3`.
- **HandBrakeCLI** in `/usr/local/bin` or `/opt/homebrew/bin`, ideally the same version as the HandBrake app the preset was exported from. The HandBrake app alone is not enough: the CLI is a separate download from [handbrake.fr](https://handbrake.fr/downloads2.php) and is also available via `brew install handbrake`.
- **The media library mounted locally**, with separate folders for movies and TV shows (by default `/Volumes/Media/Filme` and `/Volumes/Media/Serien`, e.g. an SMB share from a NAS). The worker waits and retries while they are not mounted.
- **Free disk space** for the output folder (default `~/Downloads/_ENCODED`, created automatically). Every encoded file stays there until you move it.
- **Optional: an Obsidian vault** for the to-do note. Without it, `scan` prints a warning and skips the note.
- **macOS privacy permissions.** When started by launchd, `zsh` and `HandBrakeCLI` may need access to network volumes and to the Downloads folder. Allow the prompts under System Settings → Privacy & Security, or grant Full Disk Access.

## Workflow

1. `python3 encoding.py scan` lists videos without x265/HEVC in the name. It filters out:
   - poor release types (LD, MD, TS, Telesync, CAM, TC, Line/Mic Dubbed),
   - files already encoded and waiting in `_ENCODED`,
   - files already in the queue,
   - files you skipped,
   - the legacy baseline imported from the previous script.
2. Confirm the numbered list, e.g. `python3 encoding.py queue alle -3 -7 --skip-rest`. This writes a batch file to `queue/`.
3. launchd detects the job and starts `encode.sh`. Encoding happens in `_ENCODED/.inprogress/`; a file is moved to `_ENCODED/` only after it succeeds. Results are logged in `logs/results.tsv`. The originals are never modified.
4. Check the finished files by hand and move them to the NAS (renaming them to x265).

Steps 1 and 2 can be automated, e.g. with a scheduled Claude task that scans regularly and asks for your selection via push notification. Everything also works by hand with the commands above.

## Other commands

```bash
python3 encoding.py status          # progress
python3 encoding.py skip 4          # hide a file permanently (until its name changes)
python3 encoding.py skipped         # list hidden files
python3 encoding.py unskip 2        # show it again
python3 encoding.py version         # show the installed version
zsh status.sh                       # live view
```

## Installation (macOS)

1. Install HandBrakeCLI (see [Requirements](#requirements)) and check it with `HandBrakeCLI --version`.
2. Create a folder, e.g. `~/scripts/encoding`, put all files in it, and `cd` into it.
3. **Create your configuration** and adjust the paths and preset name:
   ```bash
   cp config.example.env config.env
   ```
   `config.env` stays local and is ignored by git. See [Configuration](#configuration).
4. Enable the service. This command fills the plist template with the current folder:
   ```bash
   chmod +x encode.sh status.sh
   sed "s|/Users/YOUR_USER/scripts/encoding|$PWD|g" local.plex-encoding.plist \
     > ~/Library/LaunchAgents/local.plex-encoding.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.plex-encoding.plist
   ```
5. Optional, to take over an old history: `python3 encoding.py import-legacy <cache.json> <note.md>`

To disable: `launchctl bootout gui/$(id -u)/local.plex-encoding`

## Configuration

All machine-specific settings are in `config.env`, next to the scripts. Both `encoding.py` and `encode.sh` read it.

| Setting | Meaning |
|---|---|
| `FILME_DIR`, `SERIEN_DIR` | Movie and TV show folders of the library (read only) |
| `ENCODED_DIR` | Output folder for finished files |
| `NOTE_DIR` | Obsidian folder for the to-do and status notes. Leave empty to skip notes. |
| `PRESET_FILE` | HandBrake preset file. Leave empty to use `preset.json` next to the scripts. |
| `PRESET_NAME` | The preset's internal name (`PresetName` in the JSON) |
| `STATUS_NOTE_ENABLED` | `1` updates the status note every minute while encoding; `0` turns it off |

Write values in double quotes, and use `"$HOME"` rather than `~`. A folder setting can list several paths separated by `:`; the first one that exists is used. This is useful if the same setup runs in a second environment where the shares are mounted elsewhere.

## Using your own preset

Export a preset from the HandBrake app (Presets → Export). Point `PRESET_FILE` in `config.env` to it, or save it as `preset.json`. Then set `PRESET_NAME` to its `PresetName` value.
