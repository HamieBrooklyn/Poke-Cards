#!/usr/bin/env bash
# Stop only the staging bot (default port 8081). Does not touch production on 8080.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/data/pokepon-bot-staging.pid"
PORT="${POKEPON_WEB_PORT:-8081}"

if [[ -f "$ROOT/.env.staging" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/load-env.sh"
  load_env_file "$ROOT/.env.staging" 2>/dev/null || true
  PORT="${WEB_PORT:-$PORT}"
fi

echo "Stopping staging bot wrappers (if any)..."
pkill -f "[s]cripts/run-bot-staging.sh" 2>/dev/null || true
pkill -f "[s]cripts/start-bot-staging.sh" 2>/dev/null || true

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Stopping staging pid $old_pid"
    kill "$old_pid" 2>/dev/null || true
    sleep 2
    kill -9 "$old_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "Killing listener(s) on staging port $PORT: $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    kill -9 $pids 2>/dev/null || true
  fi
fi

echo "Done. Staging port $PORT should be free (check: lsof -i :$PORT)"
