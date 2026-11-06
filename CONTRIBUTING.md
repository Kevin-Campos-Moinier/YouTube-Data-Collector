# Contributing

Bug reports and pull requests are welcome. For a bug, open an issue with the command you ran,
the error message, your system and `smdc --version`. **Never paste an API key, a
`.env` file or collected comments** into an issue: they are public.

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[all,dev]"
pytest                                              # offline, no API key needed
```

The tests run against an in-memory fake of the YouTube API (`tests/fakeyt.py`), on Windows,
macOS and Linux in CI. A change that alters behaviour comes with a test.

The dashboard's journeys run by real clicks in Chrome, on the same fake: `SMDC_E2E=1 pytest tests/test_e2e.py` (needs
Chrome and Node 22; about two minutes, no key and no quota).

## Where things are

The README's [Code layout](README.md#code-layout) section describes each module, and
[Adding a platform](README.md#code-layout) explains how to add a source other than YouTube.
The core uses only the standard library; keep it that way (optional features go in
`[project.optional-dependencies]`).

## Rules

- `src/smdc/dashboard/ui/` holds the dashboard's stylesheet, fonts and icons; the page's own layout is in
  `src/smdc/dashboard/index.html`. The fonts and icons keep their own licences (see the README's License section).
- No API key, `.env`, database or collected data in a commit (`.gitignore` covers the usual
  places).
- Record user-visible changes under an *Unreleased* heading in `CHANGELOG.md`.
- A release bumps `version` in `pyproject.toml` and `__version__` in
  `src/smdc/__init__.py`, moves *Unreleased* to the new version in `CHANGELOG.md`,
  updates `version` and `date-released` in `CITATION.cff`, and is tagged `vX.Y.Z`.

## Releases

Versions follow [Semantic Versioning](https://semver.org/), `MAJOR.MINOR.PATCH`, chosen from what *Unreleased* holds:

- **PATCH** (2.0.0 → 2.0.1): fixes only. Nothing new to learn, nothing to change in a run file.
- **MINOR** (2.0.1 → 2.1.0): something new that older run files, commands and exports survive unchanged (a new
  option with a default, a new column added at the end, a new page). The patch number goes back to 0.
- **MAJOR** (2.x → 3.0.0): something that worked stops working as it was: a run file key, a command or an option
  renamed or removed, an export column renamed, removed or changing meaning, a different default that changes what a
  run collects. Say in *Breaking changes* what users have to do.

Making one, from an up-to-date branch with the tests passing (here and in CI):

1. Bump the version in the four places listed under Rules, and date the *Unreleased* section in `CHANGELOG.md`
   (`## [2.1.0] - YYYY-MM-DD`, a new empty *Unreleased* above it, and the comparison links at the bottom).
2. Commit (`Release 2.1.0`), then tag that commit: `git tag -a v2.1.0 -m "smdc 2.1.0"`.
3. Push the branch, then the tag: `git push`, then `git push origin v2.1.0`.
4. On GitHub, *Releases* → *Draft a new release*, choose the tag, paste the notes (the CHANGELOG section, in plain
   words), and publish. GitHub attaches the source code as `.zip` and `.tar.gz`.

For now, version 2 lives on the `v2` branch, its releases are marked *Pre-release*, and `main` keeps version 1
unchanged. When version 2 becomes the main version, `main` moves to `v2` (`git push origin v2:main`, no force needed)
and releases become regular ones.
