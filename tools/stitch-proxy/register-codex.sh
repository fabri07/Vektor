#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_NAME="${1:-stitch}"
CODEX_BIN="${CODEX_BIN:-}"

if [[ -z "$CODEX_BIN" ]] && command -v codex >/dev/null 2>&1; then
  CODEX_BIN="$(command -v codex)"
fi

if [[ -z "$CODEX_BIN" ]] && command -v zsh >/dev/null 2>&1; then
  CODEX_BIN="$(zsh -lc 'command -v codex || true')"
fi

if [[ -z "$CODEX_BIN" ]]; then
  VSCODE_CODEX="$HOME/.vscode/extensions/openai.chatgpt-26.417.40842-darwin-x64/bin/macos-x86_64/codex"
  if [[ -x "$VSCODE_CODEX" ]]; then
    CODEX_BIN="$VSCODE_CODEX"
  fi
fi

if [[ -z "$CODEX_BIN" ]]; then
  echo "Codex CLI is not available in PATH." >&2
  echo "Set CODEX_BIN=/absolute/path/to/codex and retry." >&2
  exit 1
fi

echo "Using Codex CLI: $CODEX_BIN"
"$CODEX_BIN" mcp remove "$SERVER_NAME" >/dev/null 2>&1 || true
"$CODEX_BIN" mcp add "$SERVER_NAME" -- "$SCRIPT_DIR/run.sh"
"$CODEX_BIN" mcp get "$SERVER_NAME"
