#!/usr/bin/env bash
# Publish required branch-protection checks on a release-please PR head commit.
# GITHUB_TOKEN pushes never fire pull_request workflows; use the Checks API instead.
set -euo pipefail

HEAD_SHA="${HEAD_SHA:?HEAD_SHA required}"
REPOSITORY="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required}"
CHECK_IDS_FILE="$(mktemp)"
trap 'rm -f "$CHECK_IDS_FILE"' EXIT

check_begin() {
  local name="$1"
  local id started_at
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  id="$(
    gh api "repos/${REPOSITORY}/check-runs" \
      -f name="$name" \
      -f head_sha="$HEAD_SHA" \
      -f status=in_progress \
      -f started_at="$started_at" \
      --jq .id
  )"
  printf '%s=%s\n' "$name" "$id" >>"$CHECK_IDS_FILE"
}

check_end() {
  local name="$1"
  local conclusion="$2"
  local id completed_at
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  id="$(grep -F "${name}=" "$CHECK_IDS_FILE" | head -n1 | cut -d= -f2-)"
  gh api "repos/${REPOSITORY}/check-runs/${id}" \
    --method PATCH \
    -f status=completed \
    -f conclusion="$conclusion" \
    -f completed_at="$completed_at" >/dev/null
}

run_check() {
  local name="$1"
  shift
  local rc=0
  check_begin "$name"
  "$@" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    check_end "$name" success
  else
    check_end "$name" failure
  fi
  return "$rc"
}

failures=0

run_check "Pyright" uv run pyright || failures=$((failures + 1))

run_check "Pytest (hermetic)" \
  uv run pytest -m "not integration and not browser" -n auto \
  || failures=$((failures + 1))

run_check "Pytest (browser layout)" bash -c '
  uv run playwright install --with-deps chromium
  uv run pytest -m "browser and not integration"
' || failures=$((failures + 1))

run_check "Shellcheck" bash -c '
  set -euo pipefail
  mapfile -t targets < <(
    while IFS= read -r path; do
      [[ -z "$path" ]] && continue
      first_line="$(head -n1 "$path" 2>/dev/null || true)"
      if [[ "$first_line" == *python* ]]; then
        continue
      fi
      printf "%s\n" "$path"
    done < <(
      git ls-files -- scripts \
        | grep -Ev "\.(py|md|txt|yml|yaml|json|toml)$" \
        || true
    )
  )
  if [[ ${#targets[@]} -eq 0 ]]; then
    exit 0
  fi
  shellcheck "${targets[@]}"
' || failures=$((failures + 1))

run_check "Web (typecheck + build)" bash -c '
  set -euo pipefail
  corepack enable pnpm
  cd web
  pnpm install --frozen-lockfile
  pnpm run typecheck
  pnpm run build
  test -f ../app/api/static/dist/main.js
' || failures=$((failures + 1))

exit "$failures"
