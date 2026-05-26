#!/usr/bin/env bash
# Start API (8000) + dashboard (8080). Run from repo root: ./start.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
API_PORT="${API_PORT:-8000}"
DASH_PORT="${DASH_PORT:-8080}"

free_port() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi
  echo "Port ${port} (${label}) in use — stopping pids: ${pids}"
  kill ${pids} 2>/dev/null || true
  sleep 0.4
  pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill -9 ${pids} 2>/dev/null || true
    sleep 0.3
  fi
  if lsof -ti ":${port}" >/dev/null 2>&1; then
    echo "ERROR: Could not free port ${port}. Run: lsof -ti :${port} | xargs kill -9"
    exit 1
  fi
}

cleanup() {
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" 2>/dev/null; then kill "$API_PID" 2>/dev/null || true; fi
  if [[ -n "${DASH_PID:-}" ]] && kill -0 "$DASH_PID" 2>/dev/null; then kill "$DASH_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

echo "Starting API on http://127.0.0.1:${API_PORT} …"
free_port "$API_PORT" "API"
(cd "$ROOT/api" && python3 -m uvicorn app:app --reload --host 127.0.0.1 --port "$API_PORT") &
API_PID=$!

echo "Starting dashboard on http://127.0.0.1:${DASH_PORT} …"
free_port "$DASH_PORT" "dashboard"
(cd "$ROOT/dashboard" && python3 -m http.server "$DASH_PORT" --bind 127.0.0.1) &
DASH_PID=$!

echo ""
echo "  Dashboard → http://127.0.0.1:${DASH_PORT}  (use 127.0.0.1, not localhost, if you see ERR_EMPTY_RESPONSE)"
echo "  API       → http://127.0.0.1:${API_PORT}/api/health"
echo "  Press Ctrl+C to stop both."
echo ""

wait
