# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
