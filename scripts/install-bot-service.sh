#!/usr/bin/env bash
# Install launchd service so the bot survives logout/reboot and deploy-mac can restart it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${POKEPON_LAUNCHD_LABEL:-org.pokepon.bot}"
PLIST_SRC="$ROOT/scripts/org.pokepon.bot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

# macOS blocks background launchd jobs from ~/Documents, ~/Desktop, ~/Downloads
# unless Full Disk Access is granted to the binary (unreliable). Prefer ~/Developer.
case "$ROOT" in
  "$HOME/Documents"*|$HOME/Desktop"*|$HOME/Downloads"*)
    if [[ "${ALLOW_PROTECTED_PATH:-}" != "1" ]]; then
      echo "ERROR: The repo is under a protected macOS folder:" >&2
      echo "  $ROOT" >&2
      echo >&2
      echo "launchd cannot start the bot here (Operation not permitted)." >&2
      echo "Move the repo, then reinstall, for example:" >&2
      echo "  mkdir -p \"$HOME/Developer\"" >&2
      echo "  mv \"$ROOT\" \"$HOME/Developer/Poke-Cards\"" >&2
      echo "  cd \"$HOME/Developer/Poke-Cards\" && ./scripts/install-bot-service.sh" >&2
      echo >&2
      echo "Or run the bot in Terminal instead of launchd (see docs/REMOTE-DEPLOY.md)." >&2
      echo "Override (not recommended): ALLOW_PROTECTED_PATH=1 $0" >&2
      exit 1
    fi
    echo "WARNING: installing under protected path — launchd may fail without Full Disk Access." >&2
    ;;
esac

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env — create it before installing the service." >&2
  exit 1
fi

if [[ ! -x "$ROOT/scripts/run-bot.sh" ]]; then
  chmod +x "$ROOT/scripts/run-bot.sh" "$ROOT/scripts/deploy-mac.sh"
fi

sed "s|__REPO_ROOT__|$ROOT|g" "$PLIST_SRC" >"$PLIST_DST"
"$ROOT/scripts/stop-bot.sh"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "Installed $PLIST_DST and started $LABEL"
echo "Logs: $ROOT/logs/bot.log"
echo "Deploy: $ROOT/scripts/deploy-mac.sh"
