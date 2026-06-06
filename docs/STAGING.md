# Staging environment

Use staging to implement and test features **before** touching production (`api.pokepon.org`, port **8080**, `data/poke_cards.db`).

| | Production | Staging |
|---|---|---|
| Env file | `.env` | `.env.staging` |
| Bot port | `8080` | `8081` |
| Public API | `https://api.pokepon.org` | `https://api-staging.pokepon.org` |
| Database | `data/poke_cards.db` | `data/poke_cards_staging.db` |
| Deploy script | `scripts/deploy-mac.sh` | `scripts/deploy-mac-staging.sh` |
| Stop script | `scripts/stop-bot.sh` | `scripts/stop-bot-staging.sh` |
| Logs | `logs/bot.log` | `logs/bot-staging.log` |

Production and staging can run **at the same time** on one Mac (different ports). Stopping production no longer kills staging.

---

## One-time setup

### 1. Staging Discord application

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** (e.g. “Poké Pon Staging”).
2. Create a bot user, copy **token** → `DISCORD_TOKEN` in `.env.staging`.
3. OAuth2 → add redirect: `https://api-staging.pokepon.org/auth/discord/callback`
4. Copy **Client ID** and **Client Secret** → `DISCORD_OAUTH_*` in `.env.staging`.
5. Invite the staging bot to your **test server**; set `DEV_GUILD_ID` for fast slash sync.

Do **not** reuse the production bot token in `.env.staging`.

### 2. Env file

```bash
cd /Users/hamie/Developer/Poke-Cards
bash scripts/setup-staging-env.sh
```

Opens `.env.staging` with `<<< FILL IN >>>` markers and a generated `WEB_SESSION_SECRET`. See **config/STAGING-FILL-IN.md** for where each value comes from.

### 3. Cloudflare Tunnel (second hostname)

Add a second ingress rule to `~/.cloudflared/config.yml` (same tunnel as production):

```yaml
ingress:
  - hostname: api.pokepon.org
    service: http://localhost:8080
  - hostname: api-staging.pokepon.org
    service: http://localhost:8081
  - service: http_status:404
```

In the Cloudflare dashboard for `pokepon.org`, add a **CNAME** (or tunnel route) for `api-staging` pointing at the same tunnel as `api`.

Restart cloudflared after editing:

```bash
pkill -f cloudflared || true
nohup cloudflared tunnel run pokepon-api >> /Users/hamie/Developer/Poke-Cards/logs/cloudflared.log 2>&1 &
```

Repo copy: `config/cloudflared.example.yml`.

### 4. Optional: Neon staging branch

For Postgres instead of SQLite, use a separate branch/database URL in `.env.staging` only. See `docs/NEON.md`. Never set production `DATABASE_URL` in `.env.staging`.

---

## Catalog & packs (match production)

Staging uses its own database. On first setup it only has schema — **not** the imported cards/packs from live.

After `.env.staging` exists, copy the catalog once from production:

```bash
bash scripts/seed-staging-catalog-from-prod.sh
```

That copies `cards`, `card_series`, pack links, assembly groups, and drop tables. It does **not** copy player inventories or auctions.

To re-import from the TCG API into staging instead (slow):

```bash
POKEPON_ENV_FILE=.env.staging .venv/bin/python -m poke_pon_bot.scripts.sync_catalog
POKEPON_ENV_FILE=.env.staging .venv/bin/python -m poke_pon_bot.scripts.sync_pack_series
```

---

## Daily workflow

```bash
cd /Users/hamie/Developer/Poke-Cards

# Start (migrate + background)
bash scripts/start-bot-staging.sh

# Smoke test
bash scripts/verify-staging.sh

# After code changes on your branch
bash scripts/deploy-mac-staging.sh

# Stop staging only
bash scripts/stop-bot-staging.sh
```

Foreground debugging:

```bash
bash scripts/run-bot-staging.sh
```

---

## Website against staging API

Use the **staging website** (separate deploy from production):

| | URL |
|---|---|
| Staging site | **https://staging.pokepon.org** |
| Staging API | **https://api-staging.pokepon.org** |

Setup and workflow: **`hamiebrooklyn.github.io/docs/WEBSITE_STAGING.md`**

```bash
cd ~/Documents/GitHub/hamiebrooklyn.github.io
git checkout staging
# edit files…
bash scripts/deploy-website-staging.sh
```

Promote to production when ready:

```bash
bash scripts/promote-website-to-production.sh
```

Legacy: you can still point production pokepon.org at staging API with  
`https://pokepon.org/collection/?api=https://api-staging.pokepon.org` — prefer **staging.pokepon.org** instead.

---

## Promoting to production

Only after staging verification:

1. Merge to `master` (or your production branch).
2. `bash scripts/deploy-mac.sh` — updates prod bot + prod DB migrations.
3. Push website changes to `hamiebrooklyn.github.io` `main` if needed (production `pokepon-api-base` meta stays `https://api.pokepon.org`).

See `.cursor/rules/staging-before-production.mdc` for agent workflow.
