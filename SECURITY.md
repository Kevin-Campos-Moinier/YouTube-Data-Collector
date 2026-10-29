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
  (DNS rebinding) and cross-origin `POST`s, and cannot be framed by another site.
- **Collected data**: the data folder is created readable by its owner only. Commenters are
  pseudonymized with a keyed hash whose secret stays in that folder; raw API responses are
  stored without commenter identifiers unless `keep_identifiers = true`.
- **Exports**: `--excel` CSVs neutralize cells that Excel would run as formulas.
