"""Bot client: intents, extension loading, slash command sync."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.errors import DiscordServerError, HTTPException, LoginFailure
from discord.ext import commands

from poke_pon_bot.chat_commands import chat_command_token_count, is_official_guild
from poke_pon_bot.config import Settings, load_settings
from poke_pon_bot.db.session import async_session_factory, create_engine_from_url

_ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"

if TYPE_CHECKING:
    from poke_pon_bot.web.server import WebServer

_LOG = logging.getLogger(__name__)

# Persisted across restarts so we can skip the global slash-command sync when the
# in-memory tree is identical to what Discord already has. Re-syncing on every restart
# burns the per-app **200 global syncs / day** cap and can also trip Cloudflare's login
# throttle (HTTP 429 / error code 40062 on ``GET /users/@me``) when the bot is bounced
# repeatedly. Only the hash (and the dev-guild it was synced to) needs to be remembered;
# the file is rewritten in place after a successful sync.
_SLASH_SYNC_STATE_PATH = Path("data/.slash_sync_state.json")


def _hash_command_tree(tree: discord.app_commands.CommandTree) -> str:
    """Return a stable digest of every global app command currently in ``tree``.

    Includes name, description, type, default permissions, and the recursive option
    schema so any meaningful change to a slash command (rename, new option, choice
    tweak) flips the hash and forces a re-sync, while pure code changes that don't
    alter the public command surface keep the hash stable.
    """

    def _option_payload(opt: discord.app_commands.Choice | object) -> object:
        # ``to_dict`` exists on both Command and Parameter trees in modern discord.py;
        # falling back to ``repr`` keeps the hash deterministic if a future version
        # exposes a different serializer.
        to_dict = getattr(opt, "to_dict", None)
        if callable(to_dict):
            try:
                return to_dict()
            except TypeError:
                # Some discord.py versions require a translator; pass None.
                try:
                    return to_dict(None)
                except Exception:  # pragma: no cover - defensive
                    pass
        return repr(opt)

    payload: list[object] = []
    for cmd in sorted(tree.get_commands(guild=None), key=lambda c: c.qualified_name):
        payload.append(_option_payload(cmd))
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_slash_sync_state() -> dict[str, object]:
    try:
        return json.loads(_SLASH_SYNC_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_slash_sync_state(**kwargs: object) -> None:
    try:
        _SLASH_SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SLASH_SYNC_STATE_PATH.write_text(json.dumps(kwargs))
    except OSError as exc:
        _LOG.warning("Could not persist slash-sync state to %s: %s", _SLASH_SYNC_STATE_PATH, exc)


# Local back-off lock that survives process restarts. When Discord 429s us on
# ``/users/@me`` (Cloudflare login throttle) or 503s with ``reset reason: overflow``
# (Envoy edge throttle), every additional login attempt makes the cooldown longer.
# We persist a "throttled until" timestamp so the next ``python -m poke_pon_bot``
# bails *before* hitting Discord's API again — protecting the daily login budget.
_LOGIN_COOLDOWN_PATH = Path("data/.discord_login_cooldown.json")


def _read_login_cooldown_remaining() -> float:
    try:
        data = json.loads(_LOGIN_COOLDOWN_PATH.read_text())
        until = float(data.get("throttled_until", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0
    remaining = until - time.time()
    return max(0.0, remaining)


def _record_login_throttle(seconds: float, *, reason: str) -> None:
    payload = {
        "throttled_until": time.time() + seconds,
        "recorded_at": time.time(),
        "reason": reason,
    }
    try:
        _LOGIN_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOGIN_COOLDOWN_PATH.write_text(json.dumps(payload))
    except OSError as exc:
        _LOG.warning("Could not persist Discord login cooldown: %s", exc)


def _clear_login_throttle() -> None:
    try:
        _LOGIN_COOLDOWN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _sync_database_url(async_url: str) -> str:
    """Strip the ``+aiosqlite`` driver from a SQLAlchemy URL so Alembic can use sync sqlite."""
    if "+aiosqlite" in async_url:
        return async_url.replace("sqlite+aiosqlite", "sqlite", 1)
    return async_url


def _run_alembic_upgrade_head(database_url: str) -> None:
    """Apply every pending Alembic migration in-process (synchronous; safe inside ``to_thread``).

    Closes the gap between "pulled new code" and "ran ``alembic upgrade head``" — when a
    later revision adds a column (e.g. ``user_pack_instances.guild_id``) and an existing
    SQLite file is still on the older revision, the first query that references the new
    column raises ``OperationalError: no such column``. Running the upgrade at boot makes
    schema changes apply transparently on the very next ``python -m poke_pon_bot`` restart.
    """
    from alembic import command
    from alembic.config import Config

    if not _ALEMBIC_INI_PATH.is_file():
        _LOG.warning(
            "Alembic ini not found at %s — skipping auto-upgrade. Run "
            "`alembic upgrade head` manually if you see 'no such column' errors.",
            _ALEMBIC_INI_PATH,
        )
        return

    cfg = Config(str(_ALEMBIC_INI_PATH))
    # ``alembic.ini`` ships a relative ``script_location = alembic`` — when the bot is
    # launched from a different cwd, Alembic would otherwise fail to find the revision
    # scripts. Anchor it to the repo root so it works regardless of where Python was started.
    cfg.set_main_option("script_location", str(_ALEMBIC_INI_PATH.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_database_url(database_url))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


async def _dynamic_prefix(bot: commands.Bot, message: discord.Message) -> list[str]:
    """Allow chat commands via **pp**-prefixed names (``ppcd``, ``ppdaily``, …).

    In the official server (``TUTORIAL_GUILD_ID``), bare names like ``balance`` or
    ``deck edit`` also work. Slash commands always use Discord's ``/`` menu.
    Bot mentions also work everywhere.
    """
    base = commands.when_mentioned(bot, message)
    content = (message.content or "").lstrip()
    if not content:
        return base
    allow_bare = is_official_guild(
        message.guild.id if message.guild is not None else None,
        bot.settings.tutorial_guild_id,
    )
    if chat_command_token_count(bot, content, allow_bare_names=allow_bare) > 0:
        return [*base, ""]
    return base


class PokePonBot(commands.Bot):
    """Application bot — add cogs in setup_hook and keep startup logic here."""

    def __init__(self, *, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        if settings.discord_message_content_intent:
            # Must match Developer Portal: **Bot** tab → **Privileged Gateway Intents** →
            # **Message Content Intent** (not OAuth2 URL Generator scopes, not the invite permissions grid).
            intents.message_content = True
        super().__init__(
            # Chat: **pp**-prefixed names (``ppcd``, ``ppcoll``, ``pppd`` for /pd, …).
            # Mentions also work. The dynamic prefix only treats the message as a
            # command when its first token(s) match a registered ``pp`` alias.
            command_prefix=_dynamic_prefix,
            case_insensitive=True,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.dev_guild_id = settings.dev_guild_id
        self._web_server: WebServer | None = None

        Path("data").mkdir(parents=True, exist_ok=True)
        self.engine = create_engine_from_url(settings.database_url)
        self.async_session_factory = async_session_factory(self.engine)
        if not settings.discord_message_content_intent:
            _LOG.info(
                "Message Content intent is off (DISCORD_MESSAGE_CONTENT_INTENT=0). Chat prefix commands "
                "(ppcd, ppcoll, pppd, …) will not run; use slash commands or set the intent back on in .env "
                "and Developer Portal."
            )
        else:
            _LOG.info(
                "Chat commands use **pp**-prefixed names, e.g. `ppcd`, `ppcoll`, `pppd` (Poke-duel). "
                "In the official server, bare names like `cd` or `balance` also work. "
                "Ensure Message Content Intent is enabled under Developer Portal → Bot → Privileged "
                "Gateway Intents, or message bodies will stay empty."
            )

    def attach_web_server(self, server: WebServer) -> None:
        self._web_server = server

    async def setup_hook(self) -> None:
        # Apply any pending Alembic migrations **before** the first query so a freshly
        # pulled migration (new column / table) doesn't surface as an OperationalError on
        # the first command invocation.
        try:
            await asyncio.to_thread(
                _run_alembic_upgrade_head, self.settings.database_url
            )
            _LOG.info("Alembic auto-upgrade: schema is up to date.")
        except Exception:
            _LOG.exception(
                "Alembic auto-upgrade failed — continuing with current schema. "
                "Run `alembic upgrade head` manually. If the error is "
                "'Can't locate revision', fix `alembic_version` in the DB: set it to the "
                "newest revision id that still exists under alembic/versions/, then "
                "upgrade again (or replace the DB). Otherwise you may see missing tables/columns."
            )

        # Reconcile pack series before cogs load — `/packv` reads these rows. Order:
        #   1) YAML overrides win (curated umbrellas / hand-tuned prices),
        #   2) auto-sync fills in one series per imported TCG set (Scrydex pack art when available,
        #      else pokemontcg.io logo; cached under data/.pack_art_url_cache.json),
        #   3) prune any leftover series whose code matches neither layer (and whose user packs
        #      don't reference it) so old hand-rolled rows like "sv" / "swsh" disappear cleanly.
        try:
            from poke_pon_bot.services.pack_series_loader import (
                prune_orphan_series,
                sync_pack_series_from_catalog,
                upsert_pack_series,
            )

            await upsert_pack_series(self.async_session_factory)
            await sync_pack_series_from_catalog(self.async_session_factory)
            await prune_orphan_series(self.async_session_factory)
        except (OSError, ValueError, ImportError) as exc:
            _LOG.warning("Pack series sync skipped: %s", exc)

        await self.load_extension("poke_pon_bot.cogs.general")
        await self.load_extension("poke_pon_bot.cogs.economy")
        await self.load_extension("poke_pon_bot.cogs.dev")
        await self.load_extension("poke_pon_bot.cogs.gacha")
        await self.load_extension("poke_pon_bot.cogs.grading")
        await self.load_extension("poke_pon_bot.cogs.duel")
        await self.load_extension("poke_pon_bot.cogs.trade")
        await self.load_extension("poke_pon_bot.cogs.auction")
        await self.load_extension("poke_pon_bot.cogs.packs")
        await self.load_extension("poke_pon_bot.cogs.crafting")
        await self.load_extension("poke_pon_bot.cogs.leaderboard")
        await self.load_extension("poke_pon_bot.cogs.missions")
        await self.load_extension("poke_pon_bot.cogs.tutorial")

        from poke_pon_bot.error_handlers import setup_error_handlers
        from poke_pon_bot.referral_listener import setup_referral_listener
        from poke_pon_bot.review_prompt_listener import setup_review_prompt_listener
        from poke_pon_bot.tutorial_listener import setup_tutorial_listener
        from poke_pon_bot.vote_claim_listener import setup_vote_claim_listener

        setup_error_handlers(self)
        setup_review_prompt_listener(self)
        setup_referral_listener(self)
        setup_vote_claim_listener(self)
        setup_tutorial_listener(self)

        await self._maybe_sync_slash_commands()

    async def _maybe_sync_slash_commands(self) -> None:
        """Sync slash commands to Discord — but only when something actually changed.

        Discord caps each application at ~200 global syncs / day and serves a 429
        (``error code 40062``) on ``GET /users/@me`` when the login endpoint is being
        hammered (which is what a tight restart loop looks like to Cloudflare). We
        therefore hash the in-memory command tree, compare to the last-synced hash on
        disk, and skip the network call when nothing changed.

        ``SLASH_SYNC=always`` forces a sync (use after editing a command in code);
        ``SLASH_SYNC=never`` skips it entirely (useful while you wait out a 429).
        """
        mode = self.settings.slash_sync_mode

        if mode == "never":
            _LOG.info("Slash command sync skipped (SLASH_SYNC=never).")
            return

        tree_hash = _hash_command_tree(self.tree)
        state = _read_slash_sync_state()
        same_tree = state.get("tree_hash") == tree_hash
        same_target = state.get("dev_guild_id") == self.dev_guild_id

        if mode == "auto" and same_tree and same_target:
            _LOG.info(
                "Slash command tree unchanged — skipping sync. Set SLASH_SYNC=always to force."
            )
            return

        try:
            if self.dev_guild_id is not None:
                # Mirror globals into the dev guild for instant updates while developing.
                # We only push an *empty* global list once (when we first migrated to a
                # dev-guild-only deployment) so Discord drops any stale global command
                # ids from earlier versions. After that, re-syncing globals every restart
                # would just burn the daily quota.
                guild = discord.Object(id=self.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                self.tree.clear_commands(guild=None)
                guild_cmds = await self.tree.sync(guild=guild)
                if not state.get("globals_cleared"):
                    await self.tree.sync(guild=None)
                _LOG.info(
                    "Slash: guild %s has %s commands.",
                    self.dev_guild_id,
                    len(guild_cmds),
                )
            else:
                synced = await self.tree.sync()
                _LOG.info("Slash commands synced globally (%s commands).", len(synced))
        except HTTPException as exc:
            if exc.status == 429:
                _LOG.warning(
                    "Discord throttled the slash-command sync (429). The bot will keep running "
                    "with the previously-synced command set; try again later or set "
                    "SLASH_SYNC=never until the throttle clears."
                )
                return
            raise

        _write_slash_sync_state(
            tree_hash=tree_hash,
            dev_guild_id=self.dev_guild_id,
            globals_cleared=True,
        )

    async def on_ready(self) -> None:
        # Login + identify both succeeded. Anything we recorded earlier was clearly
        # transient — drop the local cooldown so a normal restart isn't blocked.
        _clear_login_throttle()
        asyncio.create_task(self._sync_assembly_registry(), name="sync-assembly-registry")
        asyncio.create_task(self._prefetch_discord_events_cache(), name="prefetch-discord-events")

    async def _prefetch_discord_events_cache(self) -> None:
        try:
            from poke_pon_bot.services.discord_events import prefetch_discord_events_cache

            await prefetch_discord_events_cache(self, self.settings)
        except Exception:
            _LOG.exception("Discord events cache prefetch failed on startup")

    async def _sync_assembly_registry(self) -> None:
        try:
            from poke_pon_bot.services.assembly_catalog import sync_all_assemblies

            async with self.async_session_factory() as session:
                n = await sync_all_assemblies(session)
                await session.commit()
            if n:
                _LOG.info("Assembly registry: %s group(s) synced on startup.", n)
        except Exception:
            _LOG.exception("Assembly registry sync failed on startup")

    async def close(self) -> None:
        srv = self._web_server
        self._web_server = None
        if srv is not None:
            await srv.stop()
        cog = self.get_cog("GachaCog")
        if cog is not None:
            try:
                from poke_pon_bot.services.drop_recovery import flush_active_channel_drops

                await flush_active_channel_drops(
                    self.async_session_factory,
                    getattr(cog, "_active_drop_views", {}),
                )
            except Exception:
                _LOG.exception("Failed to persist active channel drops on shutdown")
        await super().close()
        await self.engine.dispose()


async def _run_bot_async() -> None:
    settings = load_settings(require_discord_token=True)

    # Refuse to even attempt login while a previous 429/503 is still cooling down.
    # Each retry inside that window extends the throttle on Discord's side, so the
    # cheapest fix is to not contact Discord at all until the timer expires.
    remaining = _read_login_cooldown_remaining()
    if remaining > 0:
        mins, secs = divmod(int(remaining), 60)
        wait_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        raise SystemExit(
            f"Discord login is on local cooldown ({wait_str} remaining).\n"
            "We set this after a previous 429 or 503 to keep the throttle from getting worse.\n"
            f"• Wait {wait_str} and try again — the cooldown clears automatically on first successful login.\n"
            "• If you're sure Discord is healthy, delete `data/.discord_login_cooldown.json` to override."
        )

    if settings.topgg_webhook_secret and settings.web_port is None:
        _LOG.warning(
            "TOPGG_WEBHOOK_SECRET is set but no listener port (WEB_PORT / TOPGG_WEBHOOK_PORT) "
            "is configured — vote webhook server not started."
        )
    bot = PokePonBot(settings=settings)
    if settings.web_port is not None:
        from poke_pon_bot.web.server import start_web_server

        server = await start_web_server(bot)
        if server is not None:
            bot.attach_web_server(server)
    assert settings.discord_token is not None
    try:
        await bot.start(settings.discord_token)
    finally:
        # Ensures Top.gg aiohttp site stops if startup fails (e.g. command sync) or on shutdown.
        if not bot.is_closed():
            await bot.close()


def run_bot() -> None:
    try:
        asyncio.run(_run_bot_async())
    except KeyboardInterrupt:
        _LOG.info("Bot stopped (Ctrl+C).")
    except LoginFailure as exc:
        raise SystemExit(
            "Discord rejected the bot token (401 Unauthorized).\n"
            "• On this PC, create/update `.env` with `DISCORD_TOKEN=` from the Developer Portal "
            "(your application → Bot → token). `.env` is not copied by git.\n"
            "• No quotes unless your shell requires them; avoid trailing spaces.\n"
            "• If you reset the token on Discord, update `.env` everywhere you run the bot."
        ) from exc
    except DiscordServerError as exc:
        # Discord's edge / upstream is shedding load for this token (typical messages:
        # ``upstream connect error... reset reason: overflow``, status 502/503/504).
        # Functionally the same as a 429: the longer we keep retrying, the longer it
        # lasts. Persist a short cooldown so the next ``python -m poke_pon_bot`` bails
        # before contacting Discord.
        _record_login_throttle(180, reason=f"http {exc.status}")
        raise SystemExit(
            f"Discord upstream returned {exc.status} ({exc.text or 'no detail'}).\n"
            "This is usually an edge throttle for this bot ('overflow' / 'overload').\n"
            "• The bot will refuse to attempt login for ~3 minutes to let the edge cool off.\n"
            "• If it persists, check status.discord.com for an outage."
        ) from exc
    except HTTPException as exc:
        # Cloudflare/login throttle. ``error code 40062`` ("Service resource is being rate
        # limited") is what Discord returns on /users/@me after too many rapid restarts.
        # The cooldown is per-token and usually clears in 5–15 minutes.
        if exc.status == 429:
            _record_login_throttle(900, reason="http 429")
            raise SystemExit(
                "Discord is rate-limiting this bot's login (HTTP 429).\n"
                "• The bot will refuse to attempt login for 15 minutes to let the throttle clear.\n"
                "• Avoid restarting in tight loops; each start counts as one login.\n"
                "• Slash command syncs now skip when the tree hasn't changed. Set "
                "SLASH_SYNC=always to force; SLASH_SYNC=never to skip while throttled."
            ) from exc
        if 500 <= exc.status < 600:
            _record_login_throttle(180, reason=f"http {exc.status}")
            raise SystemExit(
                f"Discord returned {exc.status} during login. Treating as a transient edge "
                "issue and pausing local restarts for ~3 minutes."
            ) from exc
        raise
