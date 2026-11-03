# Security

## Reporting a vulnerability

Please do **not** describe a security problem in a public issue. Open an issue titled
"Security contact" without any detail, and a maintainer will get in touch to receive the report
privately. Say which version you use (`smdc --version`).

Never include an API key, a `.env` file, a database or collected comments in a report.

## What the tool protects, and how

- **API key**: read from the environment or a git-ignored `.env` (created readable by its owner
  only); sent in a request header, never in a URL; never logged, stored in the database or included in error messages.
- **Dashboard**: listens on 127.0.0.1 only, refuses requests addressed to any other host name
  (DNS rebinding) and cross-origin `POST`s, and cannot be framed by another site. It loads nothing
  from outside: its stylesheet, fonts and icons are served from the package's own `ui/` folder. Request
  bodies are capped at 1 MB.
- **API key entered in the dashboard**: accepted only from the dashboard's own page, tested with one
  call to YouTube and written to the `.env` in the user's configuration folder, owner-only. It is never
  sent back to the page, logged or included in an error.
- **Deleting**: a deleted run's data leaves the database file itself (the file is rewritten), not just
  its tables. Exports are removed only if they are in the data folder and their manifest shows they
  belong to that run; copies moved elsewhere stay yours to delete.
- **Collected data**: the data folder is created readable by its owner only. Commenters are
  pseudonymized with a keyed hash whose secret stays in that folder; raw API responses are
  stored without commenter identifiers unless `keep_identifiers = true`.
- **Exports**: `--excel` CSVs neutralize cells that Excel would run as formulas.
