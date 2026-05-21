#!/usr/bin/env bash
set -euo pipefail
ARCH=$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/')
FALLBACK_VERSION=8.30.0
VERSION=$(curl -sf https://api.github.com/repos/gitleaks/gitleaks/releases/latest \
  | grep '"tag_name"' | cut -d'"' -f4 | tr -d 'v')
VERSION=${VERSION:-$FALLBACK_VERSION}
curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/gitleaks_${VERSION}_linux_${ARCH}.tar.gz" \
  | tar -xz gitleaks
sudo mv gitleaks /usr/local/bin/
if [[ -n "${GITLEAKS_BASE:-}" ]]; then
  gitleaks detect --source . --log-opts "${GITLEAKS_BASE}..${GITLEAKS_HEAD}"
else
  gitleaks detect --source .
fi
