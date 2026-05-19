# Releasing domesti-bot

PyPI publishing is automated from `main` via [Release Please](https://github.com/googleapis/release-please). The full checklist lives in [`PYPI_PUBLISH_PLAN.md`](PYPI_PUBLISH_PLAN.md).

## Published releases

- **0.1.0** (2026-05-19) — first PyPI release ([`domesti-bot` on PyPI](https://pypi.org/project/domesti-bot/)). Install: `pipx install domesti-bot`.

## Contributor flow

1. Land changes on `main` with [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, …).
2. Release Please opens or updates a **release PR** that bumps `[project].version` in `pyproject.toml` and `CHANGELOG.md`.
3. Merge the release PR. GitHub creates a version tag (for example `v0.2.0`) and the **Publish PyPI** workflow runs.
4. CI builds the web bundle, embeds version/commit metadata, runs `uv build`, and `uv publish`.

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
