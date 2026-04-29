#!/usr/bin/env bash
# MixedRelay v0.0.3 server launcher (bash / git-bash / WSL / Linux / macOS)
# — localhost only
# Usage:
#   ./start-mrelayd.sh
#   ./start-mrelayd.sh 127.0.0.1:6767
#   MRELAY_GO=/usr/local/go/bin/go ./start-mrelayd.sh
#
# Default binds to 127.0.0.1:6767 (loopback only). For LAN access,
# use ./start-mrelayd_shared.sh instead.
#
# All wire I/O is echoed to stdout. Channel logs / cursors / archives live
# under MRELAY_DATA (default: data/ next to the repo root).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LISTEN="${1:-${MRELAY_LISTEN:-127.0.0.1:6767}}"
DATA="${MRELAY_DATA:-data}"

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

cd "$ROOT"
echo "[mrelayd] root = $ROOT"
echo "[mrelayd] go   = $GO_BIN"
echo "[mrelayd] addr = $LISTEN"
echo "[mrelayd] data = $DATA"
echo

exec "$GO_BIN" run ./cmd/mrelayd --addr "$LISTEN" --data "$DATA"
