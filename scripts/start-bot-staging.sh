#!/usr/bin/env bash
# Migrate staging DB, then start staging bot in background (port 8081 by default).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ENV_FILE="${POKEPON_ENV_FILE:-$ROOT/.env.staging}"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/bot-staging.log"
PID_FILE="$ROOT/data/pokepon-bot-staging.pid"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Create $ENV_FILE from .env.staging.example first." >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "$ROOT/data"

# shellcheck disable=SC1091
source "$ROOT/scripts/lib/load-env.sh"
load_env_file "$ENV_FILE"
export POKEPON_ENV_FILE="$ENV_FILE"
PORT="${WEB_PORT:-8081}"

"$ROOT/scripts/stop-bot-staging.sh"

echo "Running alembic upgrade head (staging database)..."
POKEPON_ENV_FILE="$ENV_FILE" "$ROOT/.venv/bin/alembic" upgrade head

echo "Starting staging bot on port $PORT..."
nohup env POKEPON_ENV_FILE="$ENV_FILE" bash "$ROOT/scripts/run-bot-staging.sh" >>"$LOG_FILE" 2>&1 &
wrapper_pid=$!

for _ in $(seq 1 90); do
  if command -v lsof >/dev/null 2>&1; then
    py_pid="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
    if [[ -n "$py_pid" ]]; then
      echo "$py_pid" >"$PID_FILE"
      echo "Staging bot listening on :$PORT (pid $py_pid, log $LOG_FILE)"
      exit 0
    fi
  fi
  sleep 1
done

echo "$wrapper_pid" >"$PID_FILE"
echo "warn: nothing listening on :$PORT after 90s — check $LOG_FILE" >&2
exit 1
