---
description: Never leak secrets into logs, transcripts, PRs, commits, or fixtures
alwaysApply: true
---

# No secret exposure

Never leak secrets into shell/tool/transcript logs, PR/issue text, review replies,
commits, fixtures, or docs. Complements CI secret-scan (prevention vs detection).
See repository-helpers#566.

## Never print or paste

Do **not** print passwords, API keys, PATs, webhook/client secrets, tokens, private
keys, Fernet/master keys; full `.env` / `config.toml` / token-cache / auth-record
dumps; or `Authorization` headers with tokens.

## Inspecting config

Prefer allowlisted non-secret keys **or** path-existence only; redact sensitive
values. Do **not** `cat` whole configs.

```bash
# bad
cat .env
# good
test -f .env
rg -n '^(APP_ENV|LOG_LEVEL)=' .env
```

Same idea for `~/.config/*/config.toml`, token-cache, and auth-record files:
existence or allowlisted keys only — not denylist `sed` of a full dump.

## Commits and fixtures

Never commit secrets, `.env`, or private config. Never put secrets in PR text.
Fixtures use obvious fakes only.

## If leaked

Tell the operator to **rotate** the credential. Do **not** repeat the secret in
logs, replies, or follow-ups.
