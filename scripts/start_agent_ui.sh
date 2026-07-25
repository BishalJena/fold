#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UI_DIR="$REPO_ROOT/ui/agent-ui"

if [ ! -d "$UI_DIR" ]; then
  echo "ui/agent-ui not found. Run: git submodule update --init --recursive"
  exit 1
fi

if ! command -v pnpm &>/dev/null; then
  echo "pnpm is required. Install it: https://pnpm.io/installation"
  exit 1
fi

cd "$UI_DIR"
pnpm install
pnpm dev -p 3000
