# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

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
