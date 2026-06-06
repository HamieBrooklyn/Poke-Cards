#!/usr/bin/env bash
# Create or refresh .env.staging for you to paste staging Discord / Stripe credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
STAGING="$ROOT/.env.staging"
EXAMPLE="$ROOT/.env.staging.example"
PROD="$ROOT/.env"

read_env_val() {
  local key="$1" file="$2"
  [[ -f "$file" ]] || return 0
  grep -E "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$//' || true
}

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

if [[ -f "$STAGING" ]]; then
  echo "Found existing .env.staging"
  if [[ "${1:-}" != "--force" ]]; then
    echo "Re-run with --force to recreate from template (you will lose edits)."
    echo "Or edit in place: $STAGING"
    exit 0
  fi
  cp "$STAGING" "${STAGING}.bak.$(date +%Y%m%d-%H%M%S)"
  echo "Backed up previous file."
fi

SESSION_SECRET="$(gen_secret)"

# Optional: reuse test-server IDs from production (not secrets).
DEV_GUILD="$(read_env_val DEV_GUILD_ID "$PROD")"
DEVELOPER_IDS_VAL="$(read_env_val DEVELOPER_IDS "$PROD")"
TCG_KEY="$(read_env_val TCG_API_KEY "$PROD")"

cat >"$STAGING" <<EOF
# =============================================================================
# Poké Pon STAGING — fill in the lines marked <<< FILL IN >>>
# Guide: docs/STAGING.md  |  Checklist: config/STAGING-FILL-IN.md
# Start bot after editing:  bash scripts/start-bot-staging.sh
# =============================================================================

POKEPON_RUNTIME=staging

# -----------------------------------------------------------------------------
# 1) Staging Discord application (NEW app — do not use production bot token)
#    https://discord.com/developers/applications → New Application → Bot → token
# -----------------------------------------------------------------------------
DISCORD_TOKEN=<<< FILL IN: staging bot token >>>

# Application ID (same number as OAuth Client ID — Developer Portal → OAuth2 → Client ID)
DISCORD_OAUTH_CLIENT_ID=<<< FILL IN: staging application id >>>

# OAuth2 → Client Secret → Reset Secret → paste once
DISCORD_OAUTH_CLIENT_SECRET=<<< FILL IN: staging oauth client secret >>>

# Test server (right-click server → Copy Server ID). Pre-filled from .env if set.
DEV_GUILD_ID=${DEV_GUILD:-<<< FILL IN: your test server id >>>}

# Your Discord user id(s), comma-separated — /dev on staging
DEVELOPER_IDS=${DEVELOPER_IDS_VAL:-<<< FILL IN: your discord user id >>>}

# -----------------------------------------------------------------------------
# 2) Database — staging only (do not change to poke_cards.db)
# -----------------------------------------------------------------------------
DATABASE_URL=sqlite+aiosqlite:///./data/poke_cards_staging.db
CARD_SETS_CONFIG=config/card_sets.v1.yaml

# -----------------------------------------------------------------------------
# 3) Web API — port 8081 + api-staging tunnel (already in ~/.cloudflared/config.yml)
# -----------------------------------------------------------------------------
WEB_HOST=0.0.0.0
WEB_PORT=8081
WEB_PUBLIC_URL=https://api-staging.pokepon.org
WEB_FRONTEND_URL=https://pokepon.org
WEB_ALLOWED_ORIGINS=https://pokepon.org,https://hamiebrooklyn.github.io,http://127.0.0.1:5500

# Auto-generated — do not copy from production
WEB_SESSION_SECRET=${SESSION_SECRET}

# -----------------------------------------------------------------------------
# 4) Discord Portal checklist (staging app)
#    OAuth2 → Redirects → add exactly:
#      https://api-staging.pokepon.org/auth/discord/callback
#    Bot → Privileged Gateway Intents → Message Content (if you use chat commands)
#    OAuth2 URL Generator → invite staging bot to test server
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 5) Optional — copy from production .env if you use these features on staging
# -----------------------------------------------------------------------------
EOF

if [[ -n "$TCG_KEY" && "$TCG_KEY" != *"FILL IN"* ]]; then
  echo "TCG_API_KEY=${TCG_KEY}" >>"$STAGING"
else
  echo "# TCG_API_KEY=" >>"$STAGING"
fi

cat >>"$STAGING" <<'EOF'

# Stripe TEST mode only (sk_test_..., whsec_..., price_... from test dashboard)
# STRIPE_SECRET_KEY=sk_test_
# STRIPE_WEBHOOK_SECRET=whsec_
# STRIPE_PRICE_POKEDOLLARS_2500=price_
# STRIPE_PRICE_POKEDOLLARS_10000=price_
# STRIPE_PRICE_POKEDOLLARS_50000=price_
# STRIPE_PRICE_CRYSTALS_15=price_
# STRIPE_PRICE_CRYSTALS_50=price_
# STRIPE_PRICE_CRYSTALS_100=price_
# STRIPE_PRICE_HALF_DROP_COOLDOWN=price_
# STRIPE_PRICE_RANDOM_PACK=price_
EOF

chmod 600 "$STAGING" 2>/dev/null || true
mkdir -p "$ROOT/data"

echo ""
echo "Created: $STAGING"
echo ""
echo "Next steps:"
echo "  1. Open the file and replace every <<< FILL IN ... >>> line"
echo "  2. In Discord Portal (staging app), add OAuth redirect:"
echo "       https://api-staging.pokepon.org/auth/discord/callback"
echo "  3. Cloudflare DNS: CNAME api-staging → your tunnel (if not done)"
echo "  4. bash scripts/start-bot-staging.sh"
echo "  5. bash scripts/seed-staging-catalog-from-prod.sh   # cards + packs like live"
echo "  6. bash scripts/verify-staging.sh"
echo ""
echo "Website test URL:"
echo "  https://pokepon.org/collection/?api=https://api-staging.pokepon.org"
echo ""
