#!/usr/bin/env bash
# Stop dashboard (8080) and API (8000) if running.
set -euo pipefail

API_PORT="${API_PORT:-8000}"
DASH_PORT="${DASH_PORT:-8080}"

stop_port() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    echo "Port ${port} (${label}): nothing running"
    return 0
  fi
  echo "Stopping ${label} on port ${port} (pids: ${pids})"
  kill ${pids} 2>/dev/null || true
  sleep 0.3
  pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill -9 ${pids} 2>/dev/null || true
  fi
}

stop_port "$DASH_PORT" "dashboard"
stop_port "$API_PORT" "API"
echo "Done."
