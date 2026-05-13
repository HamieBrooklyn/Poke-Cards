# Poke-Cards

Discord bot using [discord.py](https://discordpy.readthedocs.io/en/stable/discord.html) with a **Pokémon Trading Card Game** gacha layer: import real printings from the [Pokémon TCG API](https://pokemontcg.io/), weight drops by rarity tier, and reveal official card artwork in Discord.

## Quick start

1. **Discord application** — Create an app and a bot user in the [Discord Developer Portal](https://discord.com/developers/applications) ([getting started](https://docs.discord.com/developers/quick-start/getting-started)).
2. **Token** — Developer Portal → your application → **Bot** → reset/copy token.
3. **Invite** — OAuth2 → URL Generator → scopes **bot** and **applications.commands** → open the generated URL and add the bot to a server.
4. **Install**

   ```bash
   cd ~/Documents/GitHub/Poke-Cards
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   cp .env.example .env
   # Edit .env — at minimum DISCORD_TOKEN=...
   ```

   You can instead `source .venv/bin/activate` and then use `python` / `pip`, but macOS often has no bare `python` on your PATH unless the venv is activated. Prefer the `.venv/bin/...` paths below so copy-paste works without activating.

   If `.venv/bin/python` is broken after moving this folder, recreate the env:

   ```bash
   rm -rf .venv && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
   ```

5. **Database** — From the repo root (creates `data/poke_cards.db`). Use Alembic **inside** the venv so `alembic` resolves:

   ```bash
   mkdir -p data
   .venv/bin/alembic upgrade head
   ```

6. **Import cards** — Curated sets live in [`config/card_sets.v1.yaml`](config/card_sets.v1.yaml). Override the path with `CARD_SETS_CONFIG` in `.env`. Optional [API key](https://pokemontcg.io/) for higher rate limits:

   ```bash
   .venv/bin/python -m poke_pon_bot.scripts.sync_catalog
   ```

7. **Run the bot**

   ```bash
   .venv/bin/python -m poke_pon_bot
   ```

   Optional: set `DEV_GUILD_ID` in `.env` so slash commands sync to your test guild quickly during development.

   **If zsh says `command not found: alembic` or `python`**, you ran system commands outside the venv — use `.venv/bin/alembic` / `.venv/bin/python` as above (or activate the venv first).

## Slash commands

| Command | Description |
|--------|-------------|
| `/ping`, `/hello` | Smoke tests |
| `/drop` | Opens a pack (grid collage); tap **one button** to keep a card; optional `private` |
| `/collection recent` | Lists your newest saved cards (ephemeral) |
| `/collection search` | Filter **name**, **rarity**, **pokedex** (#), and/or **`slot`** (1 = newest among matches); optional **`limit`** |
| `/collection show` | Full **image + details** for one owned card (nested under `/collection`) |
| `/view_collection` | Same as **`/collection show`**, but a **top-level** command so it appears when you type `/view` |

**Chat commands (`cd`, `coll`, `packd`, …):** type the command name directly in chat. Enabled by default in code if you turn on **Message Content Intent** in the Developer Portal (Bot → Privileged Gateway Intents). Set `DISCORD_MESSAGE_CONTENT_INTENT=0` in `.env` for slash-only.

**`/collection search`** filters stack together (AND). **`slot`** picks the nth result when everything is sorted newest-first — use it alone to jump to your *n*th newest card overall, or combine filters to rank within matches only.

**Finding “view” in Discord:** Subcommands live **under** `/collection` — choose **`show`** after `/collection`, or use **`/view_collection`** at the top level (easier to discover). Restart the bot after updates; without **`DEV_GUILD_ID`**, global commands can take up to ~1 hour to refresh.

## How drops work

1. Each catalog card’s printed TCG rarity is normalized into a **`rarity_classes`** tier (`common` … `chase`) — see [`poke_pon_bot/services/rarity_normalize.py`](poke_pon_bot/services/rarity_normalize.py).
2. **`drop_weights`** defines relative odds per tier for the `default` **`drop_tables`** pool.
3. **`/drop`** opens a **pack**: slot **1** is always rolled, slot **2** always appears (**100%**). Further slots appear with decreasing probability — about **10%** for a 3rd card, then **3%**, **1%**, **0.3%**, **0.1%** for slots after that (see `_EXTRA_CARD_CHANCES` in [`poke_pon_bot/services/drops.py`](poke_pon_bot/services/drops.py)). Each slot rolls a tier using `drop_weights`, then a uniform random card within that tier.
4. Card art is stitched into **one PNG grid** (two columns) for the message; only the card you **choose with a button** is inserted into **`user_card_instances`** — the rest are discarded for that opening.

### Tuning odds

Weights are seeded in [`alembic/versions/001_initial_schema.py`](alembic/versions/001_initial_schema.py). After migration you can edit live data, for example:

```sql
-- Example: make chase pulls slightly less rare (SQLite)
UPDATE drop_weights SET weight = 5 WHERE rarity_class_id = 10 AND drop_table_id = 1;
```

Use `sqlite3 data/poke_cards.db` or any SQL client against `DATABASE_URL`.

Optional overrides for exact printed strings live in **`tcg_rarity_mappings`** (reserved for DB-level overrides; normalization handles most API strings).

### Adding sets

Edit `sets:` in [`config/card_sets.v1.yaml`](config/card_sets.v1.yaml) (or point `CARD_SETS_CONFIG` at another YAML file), then rerun:

```bash
python -m poke_pon_bot.scripts.sync_catalog
```

Already-imported cards are upserted by `tcg_card_id`.

**Evolution:** Branch targets (for example Eevee) first use printings from the **same** expansion as your copy; if that set never printed a listed evolution (common), the bot picks a fallback **from any other synced set** (newest Scarlet & Violet–style block preferred). Import enough sets that those species exist somewhere.

## Moving to PostgreSQL later

Keep code changes minimal: install an async Postgres driver (for example `asyncpg`), set:

`DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:5432/DBNAME`

Generate a new Alembic revision if you need dialect-specific tweaks; the ORM models stay the same.

## Expanding later

- Add cogs under [`poke_pon_bot/cogs/`](poke_pon_bot/cogs/) and register them in [`poke_pon_bot/client.py`](poke_pon_bot/client.py) `setup_hook`.
- Prefix commands (`!…`) require the **Message Content** privileged intent if you rely on plain messages.

## Public web dashboard (optional)

The bot can also serve an HTTP API that powers `collection.html` on the [GitHub Pages site](https://hamiebrooklyn.github.io/collection.html). Players sign in with Discord OAuth (`identify` scope only) and the page renders the same `user_card_instances` rows the bot sees — with search, sort by rarity / HP / damage, and a focused card view.

It runs inside the bot process (one aiohttp app shared with the Top.gg webhook), so you only need to expose one port over HTTPS.

1. **Developer Portal → OAuth2 → General** — copy the **Client ID** (your application id) and click **Reset Secret** to copy the **Client Secret**. Add `https://<your-public-host>/auth/discord/callback` to **Redirects**.
2. Generate a session secret:

   ```bash
   openssl rand -hex 32
   ```

3. Add to `.env` (see [`.env.example`](.env.example) for the full block):

   ```bash
   WEB_PORT=8080
   WEB_PUBLIC_URL=https://your-tunnel.ngrok-free.app
   WEB_ALLOWED_ORIGINS=https://hamiebrooklyn.github.io
   WEB_FRONTEND_URL=https://hamiebrooklyn.github.io/collection.html
   WEB_SESSION_SECRET=<openssl-output>
   DISCORD_OAUTH_CLIENT_ID=<your-application-id>
   DISCORD_OAUTH_CLIENT_SECRET=<reset-secret>
   ```

4. Front the listener with HTTPS — Cloudflare Tunnel, an Nginx reverse proxy, or `ngrok http 8080` all work. The browser refuses the session cookie without HTTPS because it uses `SameSite=None`.
5. On the GitHub Pages repo (`hamiebrooklyn.github.io`), open `collection.html` and edit the meta tag to point at your API host:

   ```html
   <meta name="pokepon-api-base" content="https://your-tunnel.ngrok-free.app" />
   ```

   Commit and push; the page will start hitting your API.

If `WEB_*` / `DISCORD_OAUTH_*` are missing, the dashboard endpoints simply stay off and the bot keeps running as before.

## References

- [discord.py API / intro](https://discordpy.readthedocs.io/en/stable/discord.html)
- [Discord developers — getting started](https://docs.discord.com/developers/quick-start/getting-started)
- [Pokémon TCG API](https://pokemontcg.io/)
