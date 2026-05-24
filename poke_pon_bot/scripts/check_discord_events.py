"""One-off diagnostic: list guild scheduled events the bot can see.

Usage (from repo root):

    python -m poke_pon_bot.scripts.check_discord_events
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime

import discord
from dotenv import load_dotenv

from poke_pon_bot.config import load_settings
from poke_pon_bot.services.discord_events import (
    _is_current_event,
    fetch_public_discord_events,
    prefetch_discord_events_cache,
)

_LOG = logging.getLogger(__name__)


class _ProbeClient(discord.Client):
    def __init__(self, settings: object) -> None:
        super().__init__(intents=discord.Intents.default())
        self.settings = settings

    async def on_ready(self) -> None:
        settings = self.settings
        guild_id = int(getattr(settings, "discord_events_guild_id"))
        invite_code = getattr(settings, "discord_events_invite_code", "")

        print(f"Bot user: {self.user} ({self.user and self.user.id})")
        print(f"Configured guild_id: {guild_id}")
        print(f"Invite code: {invite_code}")
        print(f"Bot is in {len(self.guilds)} guild(s)")

        guild = self.get_guild(guild_id)
        if guild is None:
            ids = sorted(g.id for g in self.guilds)
            print(f"ERROR: bot is NOT in guild {guild_id}.")
            print(f"Guild IDs bot is in: {ids[:20]}{'…' if len(ids) > 20 else ''}")
            await self.close()
            return

        print(f"Guild: {guild.name} ({guild.id})")

        try:
            events = await guild.fetch_scheduled_events()
        except discord.Forbidden as exc:
            print(f"ERROR: Forbidden fetching events: {exc}")
            await self.close()
            return
        except discord.HTTPException as exc:
            print(f"ERROR: HTTP fetching events: {exc}")
            await self.close()
            return

        now = datetime.now(UTC)
        print(f"Raw scheduled events from Discord: {len(events)}")
        for ev in events:
            included = _is_current_event(ev, now=now)
            start = ev.start_time.isoformat() if ev.start_time else None
            end = ev.end_time.isoformat() if ev.end_time else None
            print(
                f"  - id={ev.id} name={ev.name!r} status={ev.status} "
                f"start={start} end={end} public_filter={included}"
            )

        await prefetch_discord_events_cache(self, settings)
        public = await fetch_public_discord_events(self, settings)
        print(f"Public API payload count: {len(public)}")
        for row in public:
            print(f"  -> {row['name']} ({row['status']}) {row['url']}")

        await self.close()


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    settings = load_settings()
    client = _ProbeClient(settings)
    try:
        await client.start(settings.discord_token)
    except discord.LoginFailure:
        print("ERROR: invalid DISCORD_TOKEN", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
