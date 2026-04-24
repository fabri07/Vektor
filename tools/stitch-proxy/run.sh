#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_ENV_FILE="$REPO_ROOT/backend/.env"
LOCAL_ENV_FILE="$SCRIPT_DIR/.env"

if [[ -f "$BACKEND_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BACKEND_ENV_FILE"
  set +a
fi

if [[ -f "$LOCAL_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$LOCAL_ENV_FILE"
  set +a
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 20+ is required to run the Stitch proxy." >&2
  echo "Install Node.js, then run: cd \"$SCRIPT_DIR\" && npm install" >&2
  exit 1
fi

if [[ ! -d "$SCRIPT_DIR/node_modules" ]]; then
  echo "Missing dependencies for the Stitch proxy." >&2
  echo "Run: cd \"$SCRIPT_DIR\" && npm install" >&2
  exit 1
fi

exec node "$SCRIPT_DIR/proxy.mjs"
