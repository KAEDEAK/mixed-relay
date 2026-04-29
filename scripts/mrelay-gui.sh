#!/usr/bin/env bash
# MixedRelay GUI client launcher (bash / git-bash / WSL / Linux / macOS).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PY="${MRELAY_PY:-python}"
export MRELAY_ADDR="${MRELAY_ADDR:-127.0.0.1:6767}"
export MRELAY_NICK="${MRELAY_NICK:-alice}"
export MRELAY_KIND="${MRELAY_KIND:-human}"

cd "$ROOT"
echo "[mrelay-gui] root = $ROOT"
echo "[mrelay-gui] addr = $MRELAY_ADDR"
echo "[mrelay-gui] nick = $MRELAY_NICK"
echo
exec "$PY" -m mrelay_gui
