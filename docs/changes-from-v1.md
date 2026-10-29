# Changes from version 1

Version 1 of the YouTube Data Collector ([`v1.0.0`](https://github.com/Kevin-Campos-Moinier/YouTube-Data-Collector/tree/v1.0.0))
made the key design choices that version 2 keeps: it uses the official YouTube Data API,
handles quota errors, reports what it excludes, and separates the collection engine from the
research workflow. Version 2 is a complete rewrite around those choices. This page explains,
topic by topic, what version 1 did, what version 2 does, and why it matters for a study. The
short list of changes is in [CHANGELOG.md](../CHANGELOG.md).

## Running out of quota

**Version 1** kept the collected data in memory and wrote the CSV once collection finished. When
the daily quota ran out mid-collection, the program stopped and the data collected so far was
lost, which was more likely the larger the study.

**Version 2** commits every page of comments to a SQLite database together with a checkpoint
(the API page token). Hitting the quota pauses the run; `smdc resume NAME` (or the
dashboard's *Resume*) continues from the last page after the reset, and `--wait` does it
automatically (the quota resets at midnight Pacific time).

## Non-Latin scripts

**Version 1** removed emojis with a character range that also covers Chinese, Japanese and
Korean characters. Comments in those languages became empty and were reported as "empty after
preprocessing" rather than as non-English.

**Version 2** classifies characters by their Unicode category (symbols, enclosing marks,
joiners, variation selectors), so every script survives. Removed characters become spaces, so
`great🔥game` stays two words.

## Quota cost of replies

**Version 1** called `comments.list` for every top-level comment to fetch its replies, including
comments without replies: about one extra unit per comment, the main cost of a collection.

**Version 2** requests threads with `part=snippet,replies`, which returns up to 5 replies per
thread at no extra cost, together with the total reply count. `comments.list` is only called
for threads that have more replies than that, and only up to the requested cap. On typical
videos, most threads have no replies, so most of those calls disappear.

## The original text is kept

**Version 1** replaced the comment text with its cleaned version (no emojis, no punctuation), so
tools that use emojis or punctuation (many sentiment lexicons do) could not be run afterwards,
and cleaning could not be redone differently.

**Version 2** stores the text exactly as returned and adds the processed versions as columns:
`text` (the comment on one line), `text_clean` (emojis and punctuation removed), `lang`,
`lang_conf` and `excluded_reason`. Processing can be rerun with other settings
(`smdc process`) without spending quota.

## Replies get the same processing

**Version 1** cleaned and filtered top-level comments but added replies as returned, so one
dataset mixed two processing rules.

**Version 2** processes replies like comments, and the report counts them separately.

## Short English comments

**Version 1** completed language detection with a list of common English words and
abbreviations, a good idea for short comments. Some abbreviations were stored in upper case
while the comment was lowercased before the lookup, so entries like `GG` or `IDK` never matched.

**Version 2** keeps the list, lowercased, as a fallback for short texts the statistical detector
cannot place. A comment it rescues is labelled `en`.

## Punctuation and contractions

**Version 1** removed ASCII punctuation only, so curly quotes, `…`, `«»` and `—` stayed, and it
removed apostrophes, turning `don't` into `dont` and `l'homme` into `lhomme`.

**Version 2** removes all Unicode punctuation but keeps apostrophes between letters (normalized
to `'`), which lexicon-based analyses expect.

## Oldest-first collection and the report

**Version 1** read every page of a video, filtered each page, then kept the last N rows. The
report counted every comment of the video as "initial", so on a popular video almost all of
them appeared under "Others".

**Version 2** chooses the sample first (the N oldest threads, by publication time) and computes
the report over that sample, so *collected = kept + excluded* holds exactly, for comments and
for replies.

## Reproducibility

**Version 1** asked for each parameter at interactive prompts and did not record them, and rows
carried no collection time (like counts change constantly).

**Version 2** describes a run as a TOML file (`config.example.toml`); the dashboard writes the
same file for the runs it launches. The settings are frozen in the database, every export ships
a `run_manifest.json` (settings, tool version, times, quota spent, per-video status) and a
`codebook.md`, and every row has `collected_at`.

## Commenter identifiers

**Version 1** stored each commenter's display name and channel ID. A channel ID is public and
permanent: anyone can open `youtube.com/channel/<id>`.

**Version 2** replaces commenters by a keyed hash (HMAC-SHA256 with a secret kept on the
collecting machine). The same person gets the same key across runs, so reply networks still
work, but the key cannot be linked back to an account without the secret. Display names are
dropped, `@handles` in the text become `@user`, and raw API responses are stored without
commenter identifiers. Keeping raw identifiers is an explicit opt-in (`keep_identifiers`).

## Recorded exclusions

**Version 1** logged videos with disabled comments, deleted videos and access errors to the
terminal; the CSV kept no trace of which videos were excluded or why. Every HTTP 403 error was
treated as "skip", which could drop threads for unrelated reasons.

**Version 2** checks videos before collecting (`videos.list`, 1 unit per 50 videos: title,
channel, publication date, view, like and comment counts). Every video ends with a recorded
status (`done`, `unavailable: comments_disabled`, `not_found`…), exported in `containers.csv`
and in the manifest.

## Retries

**Version 1** retried only on rate-limit errors, so a server error (500/503) or a network drop
stopped the video, and running out of retries was reported as a quota problem.

**Version 2** retries HTTP 429 and 5xx errors, network errors and rate-limit reasons with
exponential backoff and jitter. Quota errors pause the run. Other errors mark only that video
as failed, and `resume` retries it.

## API key and installation

**Version 1** read the key from `config/api_key.txt` inside the repository, which a `git add .`
could publish, and had to be run from the repository root.

**Version 2** reads the key from the environment or from a `.env` file that git ignores; the key
is never logged or stored, and error messages never contain the request URL. It installs as a
package with a `smdc` command, and double-click launchers set everything up on
Windows, macOS and Linux.

## Targets

**Version 1** accepted bare 11-character video IDs, collected a video listed twice twice, and
capped collections at 2,000 comments and 50 replies.

**Version 2** accepts any YouTube URL form, plus channels, playlists and (opt-in) searches,
drops duplicates (first occurrence wins) and has no fixed caps (0 means "all").

## A deliberate difference: what "N comments per video" means

In version 1, the language filter ran *before* counting, so "100 comments" meant 100 comments
that passed the English filter. In version 2, N is the **sample** (the first N threads in the
chosen order), and filtering labels comments inside that sample:

- The sample no longer depends on a classifier's settings. Changing `min_confidence` or the
  languages reprocesses the same comments instead of silently changing which ones were
  collected.
- The report states exactly how much the filter removed from a well-defined sample.

To end up with about N English comments, raise `max_items` accordingly; the dashboard's
filtering report shows the kept share after a first small run.

## Language detection

`langdetect`, used by version 1, is unreliable on short texts, which make up most YouTube
comments. Version 2 uses [lingua](https://github.com/pemistahl/lingua-py) restricted to a list
of candidate languages. Measured on typical comments: "this game looks great" gives en 0.68,
"yes finally" en 0.78, CJK texts 1.00. Without the restriction, "lol ok" came out as Zulu and
"can't wait" as Welsh. Very short or stretched comments ("O MA GAWD", "yessss") can still be
misplaced; the exclusion report and the dashboard show them, and `languages = []` turns the
filter off.

## Correspondence

| Version 1 | Version 2 |
|---|---|
| `YouTubeQuotaError`, `execute_with_retries` | `sources.BudgetExhausted` (pauses, resumable), `net.get_json` (backoff on 429/5xx/network) |
| `handle_youtube_http_error` | `sources.Unavailable` → per-video status stored and exported |
| `get_video_comments` (relevance, time, time_oldest) | `YouTubeSource.threads` (relevance, time, oldest) + `since`/`until` window |
| `get_replies_to_comments` | inline replies + `YouTubeSource.replies` only when needed |
| `clean_dataframe` + report | `text.process` (non-destructive) + `Store.report` (exact counts, comments and replies) |
| `collect_youtube_comments` | `pipeline.Job` (checkpointed, parallel, pause/resume) |
| `read_video_ids` (`video_ids.txt`) | `targets` in the TOML (IDs, URLs, channels, playlists, searches) |
| `get_user_input` (prompts) | TOML file, or the dashboard's form (which writes the TOML) |
| CSV `data/<name>.csv` (`;`, BOM) | `smdc export`: CSV (`--excel` for `;` + BOM), JSONL or Parquet, plus report, manifest and codebook |
| `tqdm` progress bar | CLI log + dashboard progress, quota gauge and reset countdown |
| (none) | video metadata, pseudonymization, snapshots and removed-comment detection, cost estimate, channel/playlist targets |
