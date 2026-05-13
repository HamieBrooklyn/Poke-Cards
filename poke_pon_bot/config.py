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
    # True = request privileged message content. Chat commands (`cd`, `coll`, `packd`, …)
    # need this *and* the Developer Portal toggle. Default True; set
    # DISCORD_MESSAGE_CONTENT_INTENT=0 to opt out (slash-only).
    discord_message_content_intent: bool
    """Discord monetization SKU id for “half drop cooldown” (durable one-time purchase)."""
    discord_drop_boost_sku_id: int | None
    """Discord monetization SKU id for the consumable "buy a random pack" purchase."""
    discord_pack_consumable_sku_id: int | None
    """Normal pack drop cooldown (seconds)."""
    drop_cooldown_base_seconds: float
    """Cooldown for users with an active entitlement for ``discord_drop_boost_sku_id`` (typically half of base)."""
    drop_cooldown_premium_seconds: float
    """How long to cache entitlement API results per user (seconds)."""
    drop_boost_entitlement_cache_ttl: float
    """Users who always get premium cooldown (for local testing without purchases)."""
    drop_boost_test_user_ids: frozenset[int]
    topgg_api_token: str | None
    """Public Top.gg vote page (button link in /vote)."""
    topgg_vote_url: str
    """Top.gg v1 webhook HMAC secret (``whs_…``). When set with ``topgg_webhook_port``, starts a local HTTP listener."""
    topgg_webhook_secret: str | None
    topgg_webhook_host: str
    topgg_webhook_port: int | None
    """POST path for Top.gg (e.g. ``/topgg/webhook`` — use HTTPS reverse proxy in production)."""
    topgg_webhook_path: str
    topgg_webhook_max_clock_skew_seconds: int
    """If set, reject webhooks whose ``project.platform_id`` differs (Discord application id)."""
    topgg_webhook_expected_platform_id: int | None
    """Slash command sync policy at startup: ``auto`` (only when the command tree changed),
    ``always`` (force every restart — burns the daily 200 global-sync cap fast), or ``never``
    (skip entirely; useful when Discord is throttling login)."""
    slash_sync_mode: str
    """If set, the bot also serves a public HTTP API (collection + OAuth + Top.gg webhook)
    on this host:port. When unset, only the bot itself runs. Reuses the Top.gg port if the
    Top.gg webhook is configured."""
    web_host: str
    web_port: int | None
    """Public base URL the browser uses to call the API (e.g. ``https://api.example.com``).
    Required for OAuth callback construction; falls back to ``http://<web_host>:<web_port>``."""
    web_public_url: str | None
    """Comma-separated list of allowed CORS origins for the collection API. Typically the
    GitHub Pages origin (``https://hamiebrooklyn.github.io``)."""
    web_allowed_origins: tuple[str, ...]
    """Where the browser is redirected after a successful OAuth login (and ``Logout``).
    Typically the public Collection page URL."""
    web_frontend_url: str | None
    """HMAC secret used to sign session cookies. Required to enable the OAuth flow."""
    web_session_secret: str | None
    """Session cookie lifetime in seconds (defaults to 30 days)."""
    web_session_ttl_seconds: int
    """Discord OAuth2 client id (= Discord application id)."""
    discord_oauth_client_id: int | None
    """Discord OAuth2 client secret (Developer Portal -> OAuth2 -> Reset Secret)."""
    discord_oauth_client_secret: str | None


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

    # Default **on** so chat commands (`cd`, `coll`, `packd`, …) and `@Bot cd` style mentions
    # work once the same intent is enabled in the Developer Portal (Bot → Privileged Gateway
    # Intents → Message Content Intent). Set DISCORD_MESSAGE_CONTENT_INTENT=0 to force
    # slash-only and skip requesting this intent.
    raw_mci = (os.environ.get("DISCORD_MESSAGE_CONTENT_INTENT") or "").strip().lower()
    if raw_mci in ("0", "false", "no", "off"):
        discord_message_content_intent = False
    else:
        discord_message_content_intent = True

    raw_sku = (os.environ.get("DISCORD_DROP_BOOST_SKU_ID") or "").strip()
    discord_drop_boost_sku_id: int | None
    if raw_sku:
        try:
            discord_drop_boost_sku_id = int(raw_sku)
        except ValueError as exc:
            raise SystemExit("DISCORD_DROP_BOOST_SKU_ID must be a numeric snowflake.") from exc
    else:
        discord_drop_boost_sku_id = None

    raw_pack_sku = (os.environ.get("DISCORD_PACK_CONSUMABLE_SKU_ID") or "").strip()
    discord_pack_consumable_sku_id: int | None
    if raw_pack_sku:
        try:
            discord_pack_consumable_sku_id = int(raw_pack_sku)
        except ValueError as exc:
            raise SystemExit("DISCORD_PACK_CONSUMABLE_SKU_ID must be a numeric snowflake.") from exc
    else:
        discord_pack_consumable_sku_id = None

    def _env_float(key: str, default: str) -> float:
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            return float(default)
        try:
            return float(raw)
        except ValueError as exc:
            raise SystemExit(f"{key} must be a number.") from exc

    drop_cooldown_base_seconds = _env_float("DROP_COOLDOWN_BASE_SECONDS", "600")
    drop_cooldown_premium_seconds = _env_float(
        "DROP_COOLDOWN_PREMIUM_SECONDS",
        str(drop_cooldown_base_seconds / 2.0),
    )
    if drop_cooldown_base_seconds < 1:
        raise SystemExit("DROP_COOLDOWN_BASE_SECONDS must be >= 1.")
    if drop_cooldown_premium_seconds < 1:
        raise SystemExit("DROP_COOLDOWN_PREMIUM_SECONDS must be >= 1.")
    if drop_cooldown_premium_seconds > drop_cooldown_base_seconds:
        raise SystemExit("DROP_COOLDOWN_PREMIUM_SECONDS must be <= DROP_COOLDOWN_BASE_SECONDS.")

    drop_boost_entitlement_cache_ttl = _env_float("DROP_BOOST_ENTITLEMENT_CACHE_TTL", "120")

    raw_test_u = (os.environ.get("DROP_BOOST_TEST_USER_IDS") or "").replace(",", " ")
    test_u_parts = [p for p in raw_test_u.split() if p]
    drop_boost_test_user_ids: set[int] = set()
    for p in test_u_parts:
        try:
            drop_boost_test_user_ids.add(int(p))
        except ValueError as exc:
            raise SystemExit(
                "DROP_BOOST_TEST_USER_IDS must be comma- or space-separated numeric user IDs."
            ) from exc

    topgg_api_token = (os.environ.get("TOPGG_API_TOKEN") or "").strip() or None

    raw_vote_url = (os.environ.get("TOPGG_VOTE_URL") or "").strip()
    raw_topgg_bot_id = (os.environ.get("TOPGG_BOT_ID") or "").strip()
    if raw_vote_url:
        topgg_vote_url = raw_vote_url
    elif raw_topgg_bot_id:
        try:
            topgg_vote_url = f"https://top.gg/bot/{int(raw_topgg_bot_id)}/vote"
        except ValueError as exc:
            raise SystemExit("TOPGG_BOT_ID must be a numeric Discord application / bot id.") from exc
    else:
        topgg_vote_url = "https://top.gg/bot/1496227239803748362/vote"

    topgg_webhook_secret = (os.environ.get("TOPGG_WEBHOOK_SECRET") or "").strip() or None

    topgg_webhook_host = (os.environ.get("TOPGG_WEBHOOK_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    raw_wh_port = (os.environ.get("TOPGG_WEBHOOK_PORT") or "").strip()
    topgg_webhook_port: int | None
    if raw_wh_port:
        try:
            topgg_webhook_port = int(raw_wh_port)
        except ValueError as exc:
            raise SystemExit("TOPGG_WEBHOOK_PORT must be an integer.") from exc
    else:
        topgg_webhook_port = None

    topgg_webhook_path = (os.environ.get("TOPGG_WEBHOOK_PATH") or "/topgg/webhook").strip() or "/topgg/webhook"

    raw_skew = (os.environ.get("TOPGG_WEBHOOK_MAX_CLOCK_SKEW") or "").strip()
    if raw_skew:
        try:
            topgg_webhook_max_clock_skew_seconds = int(raw_skew)
        except ValueError as exc:
            raise SystemExit("TOPGG_WEBHOOK_MAX_CLOCK_SKEW must be an integer.") from exc
    else:
        topgg_webhook_max_clock_skew_seconds = 300

    raw_sync = (os.environ.get("SLASH_SYNC") or "auto").strip().lower()
    if raw_sync in ("", "auto", "smart"):
        slash_sync_mode = "auto"
    elif raw_sync in ("always", "force", "1", "true", "yes", "on"):
        slash_sync_mode = "always"
    elif raw_sync in ("never", "skip", "0", "false", "no", "off"):
        slash_sync_mode = "never"
    else:
        raise SystemExit("SLASH_SYNC must be one of: auto, always, never.")

    web_host = (os.environ.get("WEB_HOST") or topgg_webhook_host or "0.0.0.0").strip() or "0.0.0.0"
    raw_web_port = (os.environ.get("WEB_PORT") or "").strip()
    web_port: int | None
    if raw_web_port:
        try:
            web_port = int(raw_web_port)
        except ValueError as exc:
            raise SystemExit("WEB_PORT must be an integer.") from exc
    else:
        # If Top.gg webhook server is configured, reuse its port so both APIs share one
        # listener. Otherwise the web server stays off unless WEB_PORT is set explicitly.
        web_port = topgg_webhook_port

    web_public_url = (os.environ.get("WEB_PUBLIC_URL") or "").strip() or None
    web_frontend_url = (os.environ.get("WEB_FRONTEND_URL") or "").strip() or None

    raw_origins = (os.environ.get("WEB_ALLOWED_ORIGINS") or "").strip()
    if raw_origins:
        origins = tuple(o.strip().rstrip("/") for o in raw_origins.split(",") if o.strip())
    else:
        origins = ()
    web_allowed_origins: tuple[str, ...] = origins

    web_session_secret = (os.environ.get("WEB_SESSION_SECRET") or "").strip() or None
    raw_ttl = (os.environ.get("WEB_SESSION_TTL_SECONDS") or "").strip()
    if raw_ttl:
        try:
            web_session_ttl_seconds = int(raw_ttl)
        except ValueError as exc:
            raise SystemExit("WEB_SESSION_TTL_SECONDS must be an integer.") from exc
    else:
        web_session_ttl_seconds = 60 * 60 * 24 * 30  # 30 days

    raw_oauth_id = (os.environ.get("DISCORD_OAUTH_CLIENT_ID") or "").strip()
    discord_oauth_client_id: int | None
    if raw_oauth_id:
        try:
            discord_oauth_client_id = int(raw_oauth_id)
        except ValueError as exc:
            raise SystemExit("DISCORD_OAUTH_CLIENT_ID must be a numeric snowflake.") from exc
    else:
        discord_oauth_client_id = None
    discord_oauth_client_secret = (os.environ.get("DISCORD_OAUTH_CLIENT_SECRET") or "").strip() or None

    raw_plat = (os.environ.get("TOPGG_WEBHOOK_EXPECTED_PLATFORM_ID") or "").strip()
    if raw_plat:
        try:
            topgg_webhook_expected_platform_id = int(raw_plat)
        except ValueError as exc:
            raise SystemExit("TOPGG_WEBHOOK_EXPECTED_PLATFORM_ID must be numeric.") from exc
    else:
        topgg_webhook_expected_platform_id = None

    return Settings(
        discord_token=token,
        dev_guild_id=dev_guild_id,
        developer_ids=frozenset(dev_ids),
        database_url=database_url,
        tcg_api_key=tcg_api_key,
        card_sets_config=card_sets_config,
        discord_message_content_intent=discord_message_content_intent,
        discord_drop_boost_sku_id=discord_drop_boost_sku_id,
        discord_pack_consumable_sku_id=discord_pack_consumable_sku_id,
        drop_cooldown_base_seconds=drop_cooldown_base_seconds,
        drop_cooldown_premium_seconds=drop_cooldown_premium_seconds,
        drop_boost_entitlement_cache_ttl=drop_boost_entitlement_cache_ttl,
        drop_boost_test_user_ids=frozenset(drop_boost_test_user_ids),
        topgg_api_token=topgg_api_token,
        topgg_vote_url=topgg_vote_url,
        topgg_webhook_secret=topgg_webhook_secret,
        topgg_webhook_host=topgg_webhook_host,
        topgg_webhook_port=topgg_webhook_port,
        topgg_webhook_path=topgg_webhook_path,
        topgg_webhook_max_clock_skew_seconds=topgg_webhook_max_clock_skew_seconds,
        topgg_webhook_expected_platform_id=topgg_webhook_expected_platform_id,
        slash_sync_mode=slash_sync_mode,
        web_host=web_host,
        web_port=web_port,
        web_public_url=web_public_url,
        web_allowed_origins=web_allowed_origins,
        web_frontend_url=web_frontend_url,
        web_session_secret=web_session_secret,
        web_session_ttl_seconds=web_session_ttl_seconds,
        discord_oauth_client_id=discord_oauth_client_id,
        discord_oauth_client_secret=discord_oauth_client_secret,
    )
