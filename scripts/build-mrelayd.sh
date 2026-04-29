#!/usr/bin/env bash
# MixedRelay v0.0.3 server build (bash / git-bash / WSL / Linux / macOS)
# Compiles ./cmd/mrelayd to a native binary at the repo root.
# Usage:
#   ./build-mrelayd.sh
#   MRELAY_GO=/d/dev/Go/bin/go.exe ./build-mrelayd.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve go binary: env override, then PATH.
GO_BIN="${MRELAY_GO:-}"
if [ -z "$GO_BIN" ]; then
    if command -v go >/dev/null 2>&1; then
        GO_BIN="$(command -v go)"
    fi
fi

if [ -z "$GO_BIN" ] || ! [ -x "$GO_BIN" ]; then
    echo "[ERROR] go binary not found. Set MRELAY_GO env var." >&2
    exit 1
fi

# Output name: mrelayd.exe on Windows-ish runtimes, mrelayd elsewhere.
OUT="mrelayd"
case "${OS:-}${OSTYPE:-}" in
    *Windows*|*msys*|*cygwin*|*mingw*) OUT="mrelayd.exe" ;;
esac
case "$GO_BIN" in
    *.exe) OUT="mrelayd.exe" ;;
esac

cd "$ROOT"
echo "[build] root = $ROOT"
echo "[build] go   = $GO_BIN"
echo "[build] out  = $ROOT/$OUT"
echo

"$GO_BIN" build -o "$OUT" ./cmd/mrelayd
echo "[build] OK"
