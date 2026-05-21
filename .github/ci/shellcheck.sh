#!/usr/bin/env bash
set -euo pipefail
mapfile -t targets < <(
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    first_line="$(head -n1 "$path" 2>/dev/null || true)"
    if [[ "$first_line" == *python* ]]; then
      continue
    fi
    printf '%s\n' "$path"
  done < <(
    git ls-files -- scripts \
      | grep -Ev '\.(py|md|txt|yml|yaml|json|toml)$' \
      || true
  )
)
if [[ ${#targets[@]} -eq 0 ]]; then
  exit 0
fi
shellcheck "${targets[@]}"
