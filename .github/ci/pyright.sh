#!/usr/bin/env bash
set -euo pipefail
bash .github/ci/setup-python.sh
uv run pyright
