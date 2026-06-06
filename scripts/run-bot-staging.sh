#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/load-env.sh"
ENV_FILE="${POKEPON_ENV_FILE:-$ROOT/.env.staging}"
load_env_file "$ENV_FILE"
export POKEPON_RUNTIME="${POKEPON_RUNTIME:-staging}"
export WEB_PORT="${WEB_PORT:-8081}"
exec "$ROOT/.venv/bin/python" -m poke_pon_bot
