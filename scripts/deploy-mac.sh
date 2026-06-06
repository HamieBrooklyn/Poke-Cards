#!/usr/bin/env bash
# Pull latest code, migrate DB, restart the single local bot process.
#
# Usage:
#   ./scripts/deploy-mac.sh          # always pull + restart
#   ./scripts/deploy-mac.sh --check  # only deploy if origin/<branch> moved (for cron)
#
# Requires: git, .venv, .env on the Mac that runs production.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BRANCH="${POKEPON_DEPLOY_BRANCH:-master}"
REMOTE="${POKEPON_DEPLOY_REMOTE:-origin}"
LOG_DIR="$ROOT/logs"
DEPLOY_LOG="$LOG_DIR/deploy.log"
LOCK_DIR="$ROOT/data/.deploy.lock"
LAUNCHD_LABEL="${POKEPON_LAUNCHD_LABEL:-org.pokepon.bot}"
CHECK_ONLY=false

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=true ;;
    -h|--help)
      echo "Usage: $0 [--check]"
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

acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "deploy skipped: another deploy is running ($LOCK_DIR)"
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
}

remote_sha() {
  git fetch "$REMOTE" "$BRANCH" --quiet
  git rev-parse "$REMOTE/$BRANCH"
}

local_sha() {
  git rev-parse HEAD
}

needs_deploy() {
  [[ "$(local_sha)" != "$(remote_sha)" ]]
}

git_pull() {
  log "git pull $REMOTE $BRANCH"
  git pull --ff-only "$REMOTE" "$BRANCH"
}

install_deps() {
  if [[ -f "$ROOT/requirements.txt" ]]; then
    log "pip install -r requirements.txt"
    "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
  fi
}

run_migrations() {
  log "alembic upgrade head (production database)"
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/load-env.sh"
  load_env_file "$ROOT/.env"
  "$ROOT/.venv/bin/alembic" upgrade head
}

launchd_loaded() {
  launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" &>/dev/null
}

stop_bot_processes() {
  log "stop existing bot processes"
  "$ROOT/scripts/stop-bot.sh" >>"$DEPLOY_LOG" 2>&1 || true
}

restart_bot() {
  stop_bot_processes
  if launchd_loaded; then
    log "restart via launchctl kickstart -k $LAUNCHD_LABEL"
    launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"
    return
  fi

  log "restart via scripts/run-bot.sh (background)"
  if [[ -f "$ROOT/data/pokepon-bot.pid" ]]; then
    old_pid="$(cat "$ROOT/data/pokepon-bot.pid")"
    if kill -0 "$old_pid" 2>/dev/null; then
      log "stopping pid $old_pid"
      kill "$old_pid" 2>/dev/null || true
      for _ in $(seq 1 30); do
        kill -0 "$old_pid" 2>/dev/null || break
        sleep 1
      done
      kill -9 "$old_pid" 2>/dev/null || true
    fi
  fi
  PROD_PORT="${POKEPON_WEB_PORT:-8080}"
  if [[ -f "$ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/scripts/lib/load-env.sh"
    load_env_file "$ROOT/.env" 2>/dev/null || true
    PROD_PORT="${WEB_PORT:-$PROD_PORT}"
  fi
  nohup "$ROOT/scripts/run-bot.sh" >>"$LOG_DIR/bot.log" 2>&1 &
  wrapper_pid=$!
  for _ in $(seq 1 90); do
    if command -v lsof >/dev/null 2>&1; then
      py_pid="$(lsof -ti "tcp:$PROD_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
      if [[ -n "$py_pid" ]]; then
        echo "$py_pid" >"$ROOT/data/pokepon-bot.pid"
        log "started bot pid $py_pid on :$PROD_PORT (wrapper $wrapper_pid)"
        return
      fi
    fi
    sleep 1
  done
  echo "$wrapper_pid" >"$ROOT/data/pokepon-bot.pid"
  log "warn: python bot not seen after 90s; recorded wrapper pid $wrapper_pid"
}

main() {
  acquire_lock
  if $CHECK_ONLY && ! needs_deploy; then
    exit 0
  fi
  if $CHECK_ONLY; then
    log "deploy: new commits on $REMOTE/$BRANCH"
  else
    log "deploy: forced"
    git fetch "$REMOTE" "$BRANCH" --quiet
  fi
  git_pull
  install_deps
  run_migrations
  restart_bot
  log "deploy done at $(local_sha)"
}

main "$@"
