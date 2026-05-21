#!/usr/bin/env bash
set -euo pipefail
corepack enable pnpm
cd web
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm run build
test -f ../app/api/static/dist/main.js
