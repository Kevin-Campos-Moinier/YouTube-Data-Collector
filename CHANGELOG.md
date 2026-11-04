# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- New look for the dashboard: fonts and icons served locally (nothing loaded from outside), light, dark and system
  themes with a button, a summary of figures at the top of every page, and addresses for each view (`#runs`, `#quota`).

### Added

- Delete a run, or the older snapshots a refresh leaves behind, from the dashboard or with `smdc delete`. The data
  leaves the database file itself. Exports go too unless you untick the box (`--keep-exports`); the quota history stays.
- Each snapshot has its own date for YouTube's 30-day rule: the Runs page and `smdc status` show it, with a warning
  seven days before and once overdue.
- First launch: the dashboard opens without a key and walks through getting one. Paste it in the page: it is tested
  (1 unit), saved in the configuration folder, readable by you only, and never shown again. No file to edit, no second
  launch. *Change the API key* is on the Quota page.
- The videos box says what it understood as you type (`2 videos, 1 channel`), and which line is wrong.

- New run: the videos come first, then three presets (*Quick sample*, *All comments*, *Custom*) with a sentence saying
  what they do; languages by name; the other options folded away. The draft survives a reload, the run name comes from
  the first video, and *New run from these settings* starts from an earlier run.
- A run's page: overall progress with the time left, in the tab's title too; *Stopping…* and *waiting for reset*; why
  a button is off; when the quota comes back, with *Continue automatically after the reset*.
- Results: download an export as a .zip, or copy its path, from a list of the run's exports; preview what an export
  holds; browse comments page by page, sorted by date or likes.

- `smdc init` saves the API key after testing it. `smdc doctor` checks the setup and says what to do. Each command
  says what comes next, and `--help` gives examples.
- *units*, *snapshot* and *kept* are explained where they appear, with the mouse or the keyboard.

### Changed

- The browser's Back and Forward buttons work, and every view has an address. Every field has a name for screen readers,
  errors and status changes are announced, and the quota gauge and the runs in the list can be reached by keyboard.
- Refreshing a snapshot asks for confirmation in the page.
- Changing a setting after the estimate updates the cost for free, and no longer asks to check the videos again.
- Errors from the API say what to do (key not valid, API not enabled, key restrictions, quota, no connection), in the
  dashboard and on the command line.
- A mistake in a line of targets is reported before any need for a key or quota.

### Fixed

- A missing file on the command line gave `error: 2`; it now names the file.
- The dashboard could keep showing a run as "running", with the wrong buttons, after it was paused or finished between
  two refreshes of the page.

### Security

- The dashboard refuses request bodies above 1 MB, and any request to set the API key that does not come from its own
  page.
- The `.env` holding the API key now lives in the user's configuration folder
  (`~/.config/smdc/.env`, Windows `%APPDATA%\smdc\.env`, or `SMDC_ENV`), not in the code folder,
  which is often synced or shared. The launcher creates it there; a `.env` in the working folder
  is still read, so existing setups keep working.

### Breaking changes

- Renamed Social Media Data Collector. The command and the package are now `smdc` (was
  `socialcollect`), the environment variables `SMDC_DATA`, `SMDC_VENV` and `SMDC_SECRET`
  (were `SOCIALCOLLECT_*`), the default folders `~/smdc-data` and `~/.venvs/smdc`, and the
  database `smdc.sqlite`. To keep data collected with 2.0.0: rename `SOCIALCOLLECT_SECRET` to
  `SMDC_SECRET` in `.env` if you set it (otherwise author keys change), move
  `~/socialcollect-data` to `~/smdc-data` and rename `socialcollect.sqlite` in it to
  `smdc.sqlite`. The launcher installs into `~/.venvs/smdc`; `~/.venvs/socialcollect` can be
  deleted.

### Added

- MIT license.

## [2.0.0] - 2026-10-20

A complete rewrite, named `socialcollect`. Topic-by-topic explanations of what changed since
version 1, and why, are in [docs/changes-from-v1.md](docs/changes-from-v1.md).

### Breaking changes

- Installed as a Python package with a `socialcollect` command (or double-click launchers),
  instead of running `source/main.py`.
- A run is described by a TOML file (see `config.example.toml`) or by the dashboard's form,
  instead of interactive prompts and `video_ids.txt`.
- The API key comes from the `YOUTUBE_API_KEY` environment variable or a `.env` file, instead
  of `config/api_key.txt`.
- Data goes to a SQLite database in a data folder (`~/socialcollect-data` by default) and is
  exported with `socialcollect export`. Exported CSVs use `,` (`--excel` gives `;` and a BOM),
  and the columns changed: see the `codebook.md` shipped with every export.
- Commenters are pseudonymized by default: `author_key` replaces the display name and channel
  ID.
- "N comments per video" is now the sample (the first N threads in the chosen order), before
  language filtering, which labels comments inside it.

### Added

- Checkpointed collection: every page is saved as it arrives; pause, `resume`, and
  `resume --wait` across quota resets.
- Local dashboard: plan, estimate, run, pause/resume, refresh, explore, reprocess and export.
- Quota ledger, cost estimate before running, quota page with a reset countdown and 14-day
  history.
- Video metadata (`videos.list`) and a recorded status for every video.
- Targets: any video URL form, `channel:`, `playlist:` and opt-in `search:`; duplicates are
  dropped.
- `since`/`until` date window.
- Snapshots (`refresh`): like-count changes and detection of comments no longer public.
- Coverage report: the platform's comment count broken down into collected (kept / filtered
  out), outside the sample, beyond the reply cap, and never returned by the API.
- Exports: CSV, JSONL or Parquet, plus `run_manifest.json`, `exclusion_report.json` and
  `codebook.md`.
- Non-destructive, reprocessable text pipeline; configurable target languages.
- Double-click launchers for Windows, macOS and Linux.
- `socialcollect --version`.
- Security: the API key travels in a request header, never in a URL; the dashboard refuses DNS-rebinding requests and cannot be framed; the data folder,
  `.secret` and `.env` are created readable by their owner only; raw API responses are stored
  without commenter identifiers; `--excel` CSVs neutralize formula-like cells. See
  [SECURITY.md](SECURITY.md).
- Offline test suite against an in-memory fake of the API, run on Windows, macOS and Linux.

### Fixed (compared with version 1)

- Running out of quota no longer loses the data collected so far.
- Chinese, Japanese and Korean comments are no longer erased by the emoji filter.
- Replies cost far less quota: one extra call only for threads with more than 5 replies.
- The original text is kept; cleaning adds columns instead of overwriting.
- Replies are cleaned and filtered like top-level comments.
- Upper-case abbreviations in the short-English word list now match.
- All Unicode punctuation is removed, and contractions keep their apostrophe.
- The oldest-first report counts the sample exactly.
- Excluded videos and the reason are recorded and exported.
- Server and network errors are retried; other errors fail only the video concerned.

### Changed

- Language detection uses lingua, restricted to candidate languages, instead of langdetect.

## [1.0.0] - 2026-05-21

The YouTube Data Collector by Kévin Campos-Moinier:
comments and replies of a list of videos through the YouTube Data API v3, three orders, English
filtering and an exclusion report.

[Unreleased]: https://github.com/Kevin-Campos-Moinier/YouTube-Data-Collector/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/Kevin-Campos-Moinier/YouTube-Data-Collector/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/Kevin-Campos-Moinier/YouTube-Data-Collector/tree/v1.0.0
