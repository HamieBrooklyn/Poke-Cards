"""Load settings from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    """Optional guild ID for faster slash-command sync while developing."""
    dev_guild_id: int | None


def load_settings() -> Settings:
    token = (os.environ.get("DISCORD_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and paste your bot token."
        )

    raw_guild = (os.environ.get("DEV_GUILD_ID") or "").strip()
    dev_guild_id: int | None
    if raw_guild:
        try:
            dev_guild_id = int(raw_guild)
        except ValueError as exc:
            raise SystemExit("DEV_GUILD_ID must be a numeric snowflake.") from exc
    else:
        dev_guild_id = None

    return Settings(discord_token=token, dev_guild_id=dev_guild_id)
