# poke-pon-bot

Discord bot built with [discord.py](https://discordpy.readthedocs.io/en/stable/discord.html). Layout is meant to stay small now and grow with **cogs** (see `poke_pon_bot/cogs/`).

## Quick start

1. **Discord application** — Create an app and a bot user in the [Discord Developer Portal](https://discord.com/developers/applications) ([getting started](https://docs.discord.com/developers/quick-start/getting-started)).
2. **Token** — Developer Portal → your application → **Bot** → reset/copy token.
3. **Invite** — OAuth2 → URL Generator → scopes **bot** and **applications.commands** → pick permissions you need → open the URL and add the bot to a server.
4. **This repo**

   ```bash
   cd ~/Projects/poke-pon-bot
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env
   # Edit .env: set DISCORD_TOKEN=...
   ```

   Optional: set `DEV_GUILD_ID` to your test server’s ID so slash commands sync quickly while you develop.

5. **Run**

   ```bash
   python -m poke_pon_bot
   ```

In Discord, try `/ping` and `/hello` on a server where the bot is present.

## Expanding later

- Add a new file under `poke_pon_bot/cogs/`, implement a `commands.Cog` and `async def setup(bot)`, then `await self.load_extension("poke_pon_bot.cogs.your_module")` in `PokePonBot.setup_hook` in `client.py`.
- Prefix commands (`!…`) need the **Message Content** privileged intent enabled for the bot in the Developer Portal (slash commands do not).

## Docs

- [discord.py API / intro](https://discordpy.readthedocs.io/en/stable/discord.html)
- [Discord developers — getting started](https://docs.discord.com/developers/quick-start/getting-started)
