# shellcheck shell=bash
# Source a dotenv file (export all keys). Usage: load_env_file /path/to/.env
load_env_file() {
  local f="$1"
  if [[ ! -f "$f" ]]; then
    echo "Missing env file: $f" >&2
    return 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$f"
  set +a
  return 0
}
