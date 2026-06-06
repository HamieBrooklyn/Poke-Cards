#!/usr/bin/env bash
# Install launchd service for the staging bot (port 8081, .env.staging).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${POKEPON_STAGING_LAUNCHD_LABEL:-org.pokepon.bot.staging}"
PLIST_SRC="$ROOT/scripts/org.pokepon.bot.staging.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -f "$ROOT/.env.staging" ]]; then
  echo "Missing $ROOT/.env.staging — run: bash scripts/setup-staging-env.sh" >&2
  exit 1
fi

sed "s|__REPO_ROOT__|$ROOT|g" "$PLIST_SRC" >"$PLIST_DST"
"$ROOT/scripts/stop-bot-staging.sh" || true
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "Installed $PLIST_DST and started $LABEL"
echo "Logs: $ROOT/logs/bot-staging.log"
echo "Stop: bash scripts/stop-bot-staging.sh (does not unload launchd)"
