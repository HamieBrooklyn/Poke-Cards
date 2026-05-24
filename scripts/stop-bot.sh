#!/usr/bin/env bash
# Stop launchd service and any bot process holding port 8080.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${POKEPON_LAUNCHD_LABEL:-org.pokepon.bot}"
PORT="${POKEPON_WEB_PORT:-8080}"

echo "Stopping launchd service (if loaded)..."
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

echo "Stopping python -m poke_pon_bot processes..."
pkill -f "[p]ython -m poke_pon_bot" 2>/dev/null || true
sleep 2
pkill -9 -f "[p]ython -m poke_pon_bot" 2>/dev/null || true

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "Killing listener(s) on port $PORT: $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    kill -9 $pids 2>/dev/null || true
  fi
fi

if [[ -f "$ROOT/data/pokepon-bot.pid" ]]; then
  rm -f "$ROOT/data/pokepon-bot.pid"
fi

echo "Done. Port $PORT should be free (check with: lsof -i :$PORT)"
