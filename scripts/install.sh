#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this script.' >&2
  exit 1
fi

uv tool install --upgrade --force "$REPO_ROOT"
printf '\n%s\n' 'Installed agenturm. Run: agenturm'
