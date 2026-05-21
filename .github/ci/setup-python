#!/usr/bin/env bash
set -euo pipefail
uv python install 3.14
current="$(uv run python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
required="$(tr -d '[:space:]' < .python-version | cut -d. -f1,2)"
if [[ "$current" != "$required" ]]; then
  echo "Python version mismatch: using $current but .python-version pins $required"
  exit 1
fi
uv sync --group dev
