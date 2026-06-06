# Staging setup — what you need to paste

Run once:

```bash
cd /Users/hamie/Developer/Poke-Cards
bash scripts/setup-staging-env.sh
```

Then edit `.env.staging` and replace each `<<< FILL IN ... >>>` line.

---

## Required (bot will not start web/OAuth without these)

| Variable | Where to get it |
|----------|-----------------|
| `DISCORD_TOKEN` | [Developer Portal](https://discord.com/developers/applications) → **staging** app → Bot → Reset Token |
| `DISCORD_OAUTH_CLIENT_ID` | Same app → OAuth2 → **Client ID** (same number as Application ID) |
| `DISCORD_OAUTH_CLIENT_SECRET` | Same app → OAuth2 → Reset **Client Secret** |
| `DEV_GUILD_ID` | Your test server → right-click → Copy Server ID |
| `DEVELOPER_IDS` | Your user → right-click profile → Copy User ID |

`WEB_SESSION_SECRET` is generated for you — **do not** copy from production `.env`.

---

## Discord Portal (staging application)

1. **New application** (e.g. “Poké Pon Staging”) — separate from production bot.
2. **Bot** → create bot → copy token → `DISCORD_TOKEN`.
3. **OAuth2 → Redirects** → **Add Redirect** (exact copy, no trailing slash):
   ```
   https://api-staging.pokepon.org/auth/discord/callback
   ```
   If login shows **Invalid OAuth2 redirect_uri**, this URL is missing or typo’d in the **staging** app (not production). Save changes in the Portal, then retry sign-in.
4. **OAuth2 → URL Generator** → scopes `bot`, `applications.commands` → invite to test server.
5. **Bot → Privileged Gateway Intents** — turn **ON** (required or bot exits on startup):
   - **Message Content Intent** (chat commands like `cd`, `coll`)
   - **Server Members Intent** (referrals, tutorial, member events)

---

## Infrastructure (mostly done on your Mac)

| Item | Status |
|------|--------|
| Tunnel ingress `api-staging` → `:8081` | In `~/.cloudflared/config.yml` |
| Staging DB file | Created on first `start-bot-staging.sh` migrate |
| Production untouched | Port `8080`, `.env`, `poke_cards.db` |

**Cloudflare DNS:** add `api-staging.pokepon.org` as a tunnel hostname (same tunnel as `api`) if public URL fails.

---

## After you fill in `.env.staging`

```bash
bash scripts/start-bot-staging.sh   # migrate + start on :8081
bash scripts/verify-staging.sh      # smoke test
```

Test website against staging API:

```
## Website (staging host)

Use **https://staging.pokepon.org** (separate GitHub Pages deploy). Setup: `hamiebrooklyn.github.io/docs/WEBSITE_STAGING.md`.

```env
WEB_FRONTEND_URL=https://staging.pokepon.org/collection/
WEB_ALLOWED_ORIGINS=https://staging.pokepon.org,https://pokepon.org,https://hamiebrooklyn.github.io
```

Legacy pokepon.org override (avoid for daily work):

```
https://pokepon.org/collection/?api=https://api-staging.pokepon.org
```
```

Sign in with Discord using the **staging** app (not production).

---

## Optional

- `TCG_API_KEY` — copied from `.env` by setup script if you had one.
- Stripe — uncomment `STRIPE_*` in `.env.staging` with **test** keys only (`sk_test_...`).
