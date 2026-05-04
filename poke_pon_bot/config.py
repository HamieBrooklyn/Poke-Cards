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
    """Discord user IDs (snowflakes) allowed to use ``/dev`` commands; empty disables them."""
    developer_ids: frozenset[int]
    database_url: str
    tcg_api_key: str | None
    """YAML listing Pokémon TCG set IDs to sync."""
    card_sets_config: Path
    # True = request message content (see DISCORD_MESSAGE_CONTENT_INTENT in .env.example).
    discord_message_content_intent: bool


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

    raw_dev_ids = (os.environ.get("DEVELOPER_IDS") or "").replace(",", " ")
    dev_id_parts: list[str] = [p for p in raw_dev_ids.split() if p]
    dev_ids: set[int] = set()
    for p in dev_id_parts:
        try:
            dev_ids.add(int(p))
        except ValueError as exc:
            raise SystemExit(
                "DEVELOPER_IDS must be a comma- or space-separated list of numeric Discord user IDs."
            ) from exc

    database_url = (
        os.environ.get("DATABASE_URL") or "sqlite+aiosqlite:///./data/poke_cards.db"
    ).strip()

    raw_key = (os.environ.get("TCG_API_KEY") or "").strip()
    tcg_api_key = raw_key or None

    raw_sets = (os.environ.get("CARD_SETS_CONFIG") or "").strip()
    card_sets_config = Path(raw_sets) if raw_sets else _default_card_sets_path()

    raw_mci = (os.environ.get("DISCORD_MESSAGE_CONTENT_INTENT") or "").strip().lower()
    discord_message_content_intent = raw_mci in ("1", "true", "yes", "on")

    return Settings(
        discord_token=token,
        dev_guild_id=dev_guild_id,
        developer_ids=frozenset(dev_ids),
        database_url=database_url,
        tcg_api_key=tcg_api_key,
        card_sets_config=card_sets_config,
        discord_message_content_intent=discord_message_content_intent,
    )
