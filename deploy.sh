#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${SCRIPT_DIR}/nextcloud/deploy.sh" ]]; then
  echo "[deploy] ERROR: nextcloud/deploy.sh not found" >&2
  exit 1
fi

exec bash "${SCRIPT_DIR}/nextcloud/deploy.sh" "$@"
