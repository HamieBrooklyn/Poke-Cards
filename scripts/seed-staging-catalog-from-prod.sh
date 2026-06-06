#!/usr/bin/env bash
# Copy catalog / pack reference data from production SQLite into staging.
# Does NOT copy users, inventories, auctions, trades, duels, or purchases.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROD_DB="${POKEPON_PROD_DB:-$ROOT/data/poke_cards.db}"
STAGING_DB="${POKEPON_STAGING_DB:-$ROOT/data/poke_cards_staging.db}"

if [[ ! -f "$PROD_DB" ]]; then
  echo "Production database not found: $PROD_DB" >&2
  exit 1
fi
if [[ ! -f "$STAGING_DB" ]]; then
  echo "Staging database not found: $STAGING_DB" >&2
  echo "Run: bash scripts/start-bot-staging.sh  (creates DB on first migrate)" >&2
  exit 1
fi

SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/pokepon-prod-snapshot.XXXXXX.db")"
cleanup() { rm -f "$SNAPSHOT"; }
trap cleanup EXIT

echo "Stopping staging bot (avoids SQLite locks)..."
"$ROOT/scripts/stop-bot-staging.sh" 2>/dev/null || true

echo "Snapshotting production DB (safe while prod bot is running)..."
"$ROOT/.venv/bin/python" -c '
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=120)
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    src.close()
    dst.close()
' "$PROD_DB" "$SNAPSHOT"
SNAP_ESC="${SNAPSHOT//\'/\'\'}"

echo "Copying catalog tables from production → staging..."
sqlite3 "$STAGING_DB" <<SQL
PRAGMA foreign_keys=OFF;
PRAGMA busy_timeout=60000;

DELETE FROM card_assembly_pieces;
DELETE FROM card_assembly_groups;
DELETE FROM card_series_sets;
DELETE FROM card_series;
DELETE FROM cards;
DELETE FROM tcg_rarity_mappings;
DELETE FROM drop_tables;

ATTACH DATABASE 'file:${SNAP_ESC}?mode=ro' AS src;

INSERT INTO cards SELECT * FROM src.cards;
INSERT INTO card_assembly_groups SELECT * FROM src.card_assembly_groups;
INSERT INTO card_assembly_pieces SELECT * FROM src.card_assembly_pieces;
INSERT INTO card_series SELECT * FROM src.card_series;
INSERT INTO card_series_sets SELECT * FROM src.card_series_sets;
INSERT INTO drop_tables SELECT * FROM src.drop_tables;
INSERT INTO tcg_rarity_mappings SELECT * FROM src.tcg_rarity_mappings;

DELETE FROM drop_weights;
INSERT INTO drop_weights SELECT * FROM src.drop_weights;
DELETE FROM rarity_classes;
INSERT INTO rarity_classes SELECT * FROM src.rarity_classes;

DETACH DATABASE src;
PRAGMA foreign_keys=ON;
SQL

echo ""
echo "Staging catalog counts:"
sqlite3 "$STAGING_DB" <<'SQL'
.mode column
SELECT 'cards' AS tbl, COUNT(*) AS n FROM cards
UNION ALL SELECT 'card_series', COUNT(*) FROM card_series
UNION ALL SELECT 'card_series_sets', COUNT(*) FROM card_series_sets
UNION ALL SELECT 'card_assembly_groups', COUNT(*) FROM card_assembly_groups;
SQL

echo ""
echo "Restarting staging bot..."
"$ROOT/scripts/start-bot-staging.sh"

echo ""
echo "Done. Staging website should now show the same catalog/packs as production."
echo "Player inventories on staging stay separate (empty unless you used /dev)."
