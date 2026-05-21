#!/usr/bin/env bash
set -euo pipefail
bash .github/ci/setup-python.sh
uv run playwright install --with-deps chromium
uv run pytest -m "browser and not integration"
