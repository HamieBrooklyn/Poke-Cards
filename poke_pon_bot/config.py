"""Load settings from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    discord_token: str | None
    """Optional guild ID for faster slash-command sync while developing."""
    dev_guild_id: int | None
    database_url: str
    tcg_api_key: str | None
    """YAML listing Pokémon TCG set IDs to sync."""
    card_sets_config: Path


def _default_card_sets_path() -> Path:
    return Path("config/card_sets.v1.yaml")


def load_settings(*, require_discord_token: bool = True) -> Settings:
    token = (os.environ.get("DISCORD_TOKEN") or "").strip() or None

    if require_discord_token and not token:
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

    database_url = (
        os.environ.get("DATABASE_URL") or "sqlite+aiosqlite:///./data/poke_cards.db"
    ).strip()

    raw_key = (os.environ.get("TCG_API_KEY") or "").strip()
    tcg_api_key = raw_key or None

    raw_sets = (os.environ.get("CARD_SETS_CONFIG") or "").strip()
    card_sets_config = Path(raw_sets) if raw_sets else _default_card_sets_path()

    return Settings(
        discord_token=token,
        dev_guild_id=dev_guild_id,
        database_url=database_url,
        tcg_api_key=tcg_api_key,
        card_sets_config=card_sets_config,
    )
