#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
if [[ -f "$ROOT/alembic.ini" ]]; then
  "$ROOT/.venv/bin/alembic" upgrade head
fi
exec "$ROOT/.venv/bin/python" -m poke_pon_bot
