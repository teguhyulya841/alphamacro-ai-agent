# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
"Report a vulnerability" (Security tab → Advisories) or email the maintainer.
Include steps to reproduce and potential impact. You can expect an initial response
within 7 days.

## Handling secrets

- API keys and the Telegram token live only in `.env` / environment variables.
- `.env` is git-ignored. If a secret is ever pushed, revoke and rotate it immediately;
  deleting the commit is not enough because it remains in Git history and forks.
- Logs redact configured secrets, but do not paste raw logs publicly without review.

## Supported versions

Only the latest release receives security fixes.
