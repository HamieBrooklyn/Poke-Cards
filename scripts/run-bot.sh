#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ENV_FILE="${POKEPON_ENV_FILE:-$ROOT/.env}"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/load-env.sh"
load_env_file "$ENV_FILE"
# Migrations run in deploy-mac.sh and again in setup_hook (fast-path when already at head).
# Skipping alembic here avoids a long window where deploy restarts can SIGKILL a duplicate upgrade.
exec "$ROOT/.venv/bin/python" -m poke_pon_bot
