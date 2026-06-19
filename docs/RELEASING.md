# Releasing domesti-bot

PyPI publishing is automated from `main` via [Release Please](https://github.com/googleapis/release-please). The full checklist lives in [`PYPI_PUBLISH_PLAN.md`](PYPI_PUBLISH_PLAN.md).

## Published releases

Release history lives in [`CHANGELOG.md`](../CHANGELOG.md). The installable version is always
[`[project].version`](../pyproject.toml) on `main` after a release PR merges, mirrored on
[PyPI](https://pypi.org/project/domesti-bot/).

The README PyPI badge (`https://img.shields.io/pypi/v/domesti-bot`) tracks PyPI automatically —
no manual bump in git. After each publish, CI runs `scripts/verify-pypi-release
--check-shields-badge` to confirm PyPI and the badge match `pyproject.toml`.

## Merge strategy (avoid duplicate changelog lines)

Release Please walks **every** commit on `main` since the last tag. If a PR is merged with **Create a merge commit**, GitHub records both the branch commit (e.g. `docs: …`) and a merge commit whose body repeats that line. Release Please treats them as two changes, so the release PR lists the same item twice in **`CHANGELOG.md` and the PR description** ([upstream discussion](https://github.com/googleapis/release-please/issues/2476)).

This repository allows **squash merge only** (merge commits and rebase merges are disabled in GitHub settings). Squash uses the PR title as the commit subject and an empty squash body (`squash_merge_commit_message: BLANK`), which matches the assert step in `.github/workflows/release-please.yml`.

The Graphite merge queue on `main` must use **squash** as its merge strategy (not merge commits). See [`docs/GRAPHITE.md`](GRAPHITE.md).

Duplicate lines in [release PR #112](https://github.com/the-hcma/domesti-bot/pull/112) came from merge commits on `main` before squash-only was enforced.

## Contributor flow

1. Land changes on `main` with [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, …).
2. Release Please opens or updates a **release PR** that bumps `[project].version` in `pyproject.toml` and `CHANGELOG.md`.
3. Merge the release PR. GitHub creates a version tag (for example `v1.2.0`) and the **Publish PyPI** workflow runs.
4. CI builds the web bundle, embeds version/commit metadata, runs `uv build`, `uv publish`, then **`scripts/verify-pypi-release --check-shields-badge`**.

## Build metadata

Wheels and sdists built for PyPI run `scripts/embed_build_metadata.py` with:

- `DOMESTI_EMBED_VERSION` — copied from `[project].version` in `pyproject.toml`
- `DOMESTI_EMBED_COMMIT` — the release tag SHA (12 hex chars)

That populates `app/_build_metadata.py` so `get_build_info()`, `GET /v1/meta`, and `--version` stay accurate without a `.git` checkout.

## Local wheel smoke test

```bash
cd web && pnpm install --frozen-lockfile && pnpm run build
export DOMESTI_EMBED_VERSION="$(python -c 'import tomllib; print(tomllib.load(open("../pyproject.toml","rb"))["project"]["version"])')"
export DOMESTI_EMBED_COMMIT="$(git rev-parse HEAD)"
uv run python scripts/embed_build_metadata.py
uv build
uv run pip install dist/domesti_bot-*.whl --force-reinstall
domesti-bot --version
domesti-bot-server --version
```

## Operator install (after publish)

```bash
pipx install domesti-bot
domesti-bot-server --listen-all   # optional LAN bind; set DOMESTI_API_KEY on untrusted networks
```

Secrets, discovery cache paths, and systemd installs remain operator concerns — PyPI does not replace `setup-service`.
