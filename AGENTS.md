# AGENTS.md

## Cursor Cloud specific instructions

This is a **Discord bot** (Python 3.12+, discord.py) with a Pokémon TCG gacha system. There is no frontend web app to test in a browser — the primary interface is Discord slash commands.

### Services

| Service | How to run | Notes |
|---------|-----------|-------|
| Bot | `.venv/bin/python -m poke_pon_bot` | Requires `DISCORD_TOKEN` in `.env` |
| Catalog sync | `CARD_SETS_CONFIG=... .venv/bin/python -m poke_pon_bot.scripts.sync_catalog` | Imports cards from Pokémon TCG API into SQLite |
| DB migrations | `.venv/bin/alembic upgrade head` | Auto-runs on bot startup via `setup_hook` |

### Key development caveats

- **No test suite exists.** There are no unit/integration tests; validation is done by running the bot and using Discord commands.
- **No linter is configured.** Use `python -m compileall poke_pon_bot/ -q` for syntax checking, or install `ruff`/`flake8` ad-hoc.
- **DISCORD_TOKEN is required** to start the bot. Without it, the process exits with a clear error. The token must be set in `.env` (see `.env.example`).
- **Database auto-migrates on startup.** The bot runs `alembic upgrade head` in `setup_hook`, so manual migration is only needed if running Alembic CLI directly.
- **Catalog sync can be slow** with `all_sets: true` (default config). For quick testing, override with a small YAML file, e.g. `CARD_SETS_CONFIG=/tmp/small.yaml` containing just `sets: [base1]`.
- **SQLite is the default DB** at `data/poke_cards.db`. The `data/` directory is gitignored and created automatically.
- **Slash command sync rate limits**: Discord caps at ~200 global syncs/day. Set `SLASH_SYNC=never` in `.env` if throttled (HTTP 429).
