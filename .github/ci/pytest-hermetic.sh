#!/usr/bin/env bash
set -euo pipefail
bash .github/ci/setup-python.sh
uv run pytest -m "not integration and not browser" -n auto
