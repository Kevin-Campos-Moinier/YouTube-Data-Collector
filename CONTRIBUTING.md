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

## Where things are

The README's [Code layout](README.md#code-layout) section describes each module, and
[Adding a platform](README.md#code-layout) explains how to add a source other than YouTube.
The core uses only the standard library; keep it that way (optional features go in
`[project.optional-dependencies]`).

## Rules

- No API key, `.env`, database or collected data in a commit (`.gitignore` covers the usual
  places).
- Record user-visible changes under an *Unreleased* heading in `CHANGELOG.md`.
- A release bumps `version` in `pyproject.toml` and `__version__` in
  `src/smdc/__init__.py`, moves *Unreleased* to the new version in `CHANGELOG.md`,
  updates `version` and `date-released` in `CITATION.cff`, and is tagged `vX.Y.Z`.
