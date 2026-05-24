# Neon PostgreSQL setup

Use [Neon](https://neon.tech) so player data lives in a hosted database instead of `data/poke_cards.db` on your Mac.

## 1. Create the database

1. Sign in at [console.neon.tech](https://console.neon.tech).
2. **New project** (e.g. `pokepon`).
3. Open the project → **Connect**.
4. Copy the connection string. Prefer:
   - **Direct** connection for the first migration (`alembic upgrade head`).
   - **Pooled** (`-pooler` in the hostname) for the long-running bot after schema exists.

Neon usually gives a URL like:

```text
postgresql://USER:PASSWORD@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require
```

## 2. Configure the bot

In `.env` (never commit this file):

```env
# Paste Neon’s URL, or use +asyncpg explicitly (both work):
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require
```

Install drivers (once per venv):

```bash
cd /Users/hamie/Documents/GitHub/Poke-Cards
.venv/bin/pip install -r requirements.txt
```

## 3. Create tables (migrations)

From the repo root, with `.env` loaded:

```bash
.venv/bin/alembic upgrade head
```

The bot also runs this on startup, but running it once yourself confirms Neon credentials before starting Discord.

Check connectivity:

```bash
.venv/bin/python -m poke_pon_bot.scripts.check_db
```

You should see `async OK: 1` and an `alembic revision` after migrations.

## 4. Start the bot

```bash
.venv/bin/python -m poke_pon_bot
```

Then sync the catalog if this is a **new** empty database:

```bash
.venv/bin/python -m poke_pon_bot.scripts.sync_catalog
```

## 5. Optional: copy data from local SQLite

If you already have players/cards in `data/poke_cards.db` and want to keep them:

1. Finish Neon migrations on an **empty** Neon DB.
2. Use a one-off tool (e.g. `pgloader`, or export/import per table). There is no built-in script yet — ask if you want a small migration helper added.

For a **fresh** Neon project, skip this step.

## Dev vs production

| Environment | Suggestion |
|-------------|------------|
| Local experiments | Keep `sqlite+aiosqlite:///./data/poke_cards.db` in a separate `.env.local` |
| Production (`api.pokepon.org`) | Neon `DATABASE_URL` on the machine that runs the bot |
| Testing tutorial resets | Use a **second** Neon branch or project so you do not wipe real users |

Neon **branches** are copy-on-write DB snapshots — handy for “staging” without a second billable project.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No module named 'asyncpg'` | `pip install -r requirements.txt` |
| SSL / connection refused | Ensure `?sslmode=require` is on the URL |
| `alembic_version` missing | Run `alembic upgrade head` |
| Timeouts on migrate | Use Neon’s **direct** host, not `-pooler`, for DDL |
| Bot works, website empty | New DB needs `sync_catalog` and fresh user data |
