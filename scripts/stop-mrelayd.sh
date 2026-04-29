#!/usr/bin/env bash
# Stop MixedRelay server. Cross-platform best-effort.
# Usage:
#   ./stop-mrelayd.sh
#   ./stop-mrelayd.sh 6767
set -uo pipefail

PORT="${1:-${MRELAY_PORT:-6767}}"

found_pids=""

# 1) Linux/macOS: lsof
if command -v lsof >/dev/null 2>&1; then
    found_pids="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
fi

# 2) Linux: ss
if [ -z "$found_pids" ] && command -v ss >/dev/null 2>&1; then
    found_pids="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $0}' \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)"
fi

# 3) Windows (git-bash): netstat + taskkill
if [ -z "$found_pids" ] && command -v netstat >/dev/null 2>&1; then
    pids_win="$(netstat -ano -p TCP 2>/dev/null \
        | grep -E "[: ]$PORT[ ].*LISTENING" \
        | awk '{print $NF}' | sort -u)"
    if [ -n "$pids_win" ]; then
        for pid in $pids_win; do
            echo "[stop-mrelayd] killing PID $pid listening on :$PORT (taskkill)"
            if command -v taskkill >/dev/null 2>&1; then
                taskkill //PID "$pid" //F >/dev/null 2>&1 \
                    && echo "[stop-mrelayd] PID $pid terminated" \
                    || echo "[stop-mrelayd] taskkill failed for PID $pid" >&2
            else
                kill -9 "$pid" 2>/dev/null && echo "[stop-mrelayd] PID $pid terminated"
            fi
        done
        exit 0
    fi
fi

if [ -z "$found_pids" ]; then
    echo "[stop-mrelayd] no listener on port $PORT"
    exit 1
fi

for pid in $found_pids; do
    echo "[stop-mrelayd] killing PID $pid listening on :$PORT"
    if kill -TERM "$pid" 2>/dev/null; then
        sleep 0.3
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        echo "[stop-mrelayd] PID $pid terminated"
    else
        echo "[stop-mrelayd] failed to kill PID $pid" >&2
    fi
done
exit 0
