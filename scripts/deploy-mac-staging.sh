#!/usr/bin/env bash
# Pull latest code, migrate staging DB, restart staging bot only (never production).
#
# Usage:
#   ./scripts/deploy-mac-staging.sh
#   ./scripts/deploy-mac-staging.sh --no-pull   # restart staging with current tree

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BRANCH="${POKEPON_STAGING_DEPLOY_BRANCH:-${POKEPON_DEPLOY_BRANCH:-master}}"
REMOTE="${POKEPON_DEPLOY_REMOTE:-origin}"
LOG_DIR="$ROOT/logs"
DEPLOY_LOG="$LOG_DIR/deploy-staging.log"
NO_PULL=false

for arg in "$@"; do
  case "$arg" in
    --no-pull) NO_PULL=true ;;
    -h|--help)
      echo "Usage: $0 [--no-pull]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

mkdir -p "$LOG_DIR" "$ROOT/data"

log() {
  printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$DEPLOY_LOG"
}

if ! $NO_PULL; then
  log "git pull $REMOTE $BRANCH"
  git pull --ff-only "$REMOTE" "$BRANCH"
  if [[ -f "$ROOT/requirements.txt" ]]; then
    log "pip install -r requirements.txt"
    "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
  fi
fi

log "restart staging bot"
"$ROOT/scripts/start-bot-staging.sh" >>"$DEPLOY_LOG" 2>&1
log "staging deploy done at $(git rev-parse HEAD)"
