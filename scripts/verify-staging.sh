#!/usr/bin/env bash
# Quick smoke checks for the staging stack.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8081
PUBLIC_URL="https://api-staging.pokepon.org"

if [[ -f "$ROOT/.env.staging" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/load-env.sh"
  load_env_file "$ROOT/.env.staging" 2>/dev/null || true
  PORT="${WEB_PORT:-8081}"
  PUBLIC_URL="${WEB_PUBLIC_URL:-$PUBLIC_URL}"
fi

fail=0
check() {
  local label="$1"
  local url="$2"
  if curl -sf --max-time 8 "$url" >/dev/null; then
    echo "OK  $label — $url"
  else
    echo "FAIL $label — $url"
    fail=1
  fi
}

echo "Staging verification (port $PORT)"
check "localhost API" "http://127.0.0.1:${PORT}/api/events"
if [[ "$PUBLIC_URL" != http://127.0.0.1* ]]; then
  check "public tunnel" "${PUBLIC_URL%/}/api/events"
fi

if pgrep -qf "poke_pon_bot" >/dev/null; then
  echo "INFO python poke_pon_bot process(es) running (prod + staging may both be up)"
else
  echo "WARN no poke_pon_bot process found"
fi

exit "$fail"
