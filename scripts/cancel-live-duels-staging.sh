#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/cancel-live-duels.py" --staging "$@"
