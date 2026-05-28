"""Single aiohttp server bootstrap for the bot's public HTTP surface.

Hosts (each only when its config is present):

* ``POST /topgg/webhook``               — Top.gg ``vote.create`` webhook.
* ``GET  /auth/discord/login``          — Start the Discord OAuth2 flow.
* ``GET  /auth/discord/callback``       — Finish OAuth, issue a session cookie.
* ``POST /auth/logout``                 — Clear the session cookie.
* ``GET  /api/me``                      — Current session payload (if any).
* ``GET  /api/me/collection``           — Signed-in user's bot card collection (summary rows).
* ``GET  /api/me/collection/evolution-sections`` — Evolution-line matches for a search query.
* ``GET  /api/me/cards/{public_id}``    — One owned card's detail.
* ``POST /api/me/cards/{public_id}/sell`` — Sell to shop (same quote rules as ``/colv``).
* ``GET  /api/me/balances``             — Signed-in user's Pokedollars and Crystals.
* ``GET  /api/auctions``                — Browse/search active auctions.
* ``GET  /api/auctions/{id}``           — Auction detail + bid history.
* ``POST /api/auctions``                — Create listing (session cookie).
* ``POST /api/auctions/{id}/bid``       — Place bid (session cookie).
* ``POST /api/me/trades``              — Create trade invite.
* ``GET  /api/me/trades``              — List user's trade sessions.
* ``GET  /api/me/trades/{id}``         — Trade session detail (poll endpoint).
* ``POST /api/me/trades/{id}/accept``  — Accept invite.
* ``POST /api/me/trades/{id}/decline`` — Decline invite.
* ``POST /api/me/trades/{id}/update``  — Update caller's side.
* ``POST /api/me/trades/{id}/ready``   — Toggle ready (executes if both ready).
* ``POST /api/me/trades/{id}/cancel``  — Cancel trade.
* ``GET  /api/me/trades/pending-count``— Incoming invite count (badge).
* ``GET  /api/me/trade-user-search``   — Autocomplete: Discord users in bot servers (``q``).
* ``GET  /api/me/settings``            — Notification preferences.
* ``PATCH /api/me/settings``           — Update notification preferences.
* ``GET  /api/me/referrals``           — Referral dashboard (invited friends + progress).
* ``GET  /api/leaderboards``           — Global rankings (strongest, tankiest, rarest, auctions).
* ``GET  /api/events``                 — Upcoming Discord scheduled events (home page panel).
* ``GET  /api/shop/catalog``           — Shop SKUs (currency, perks) + balances when signed in.
* ``POST /api/shop/checkout``          — Start Stripe Checkout (session cookie).
* ``POST /api/stripe/webhook``         — Stripe ``checkout.session.completed`` fulfillment.

Everything runs in one ``aiohttp.web.Application`` so the bot needs only one
listening port / one reverse proxy / one ngrok tunnel exposed to the internet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import web

_LOG = logging.getLogger(__name__)


@dataclass
class WebServer:
    runner: web.AppRunner
    site: web.TCPSite

    async def stop(self) -> None:
        await self.site.stop()
        await self.runner.cleanup()


def _cors_middleware(allowed_origins: tuple[str, ...]):
    """Permissive CORS for the configured frontend origin(s) only.

    Browsers refuse credentialed cross-origin requests unless we echo the
    exact origin and include ``Access-Control-Allow-Credentials: true``; a
    wildcard ``*`` would silently break the OAuth session cookie path.
    """
    allowed = {o.rstrip("/") for o in allowed_origins}

    @web.middleware
    async def middleware(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        origin = (request.headers.get("Origin") or "").rstrip("/")
        is_allowed = bool(origin) and origin in allowed

        if request.method == "OPTIONS":
            resp: web.StreamResponse = web.Response(status=204)
        else:
            resp = await handler(request)

        if is_allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, OPTIONS"
            # `ngrok-skip-browser-warning` is allowed so the frontend can bypass
            # ngrok-free's HTML interstitial without that triggering a preflight
            # failure on non-CORS-safelisted headers.
            resp.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, ngrok-skip-browser-warning"
            )
            resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    return middleware


def _secure_cookies(settings: Any) -> bool:
    return (settings.web_public_url or "").lower().startswith("https://")


async def start_web_server(bot: Any) -> WebServer | None:
    settings = bot.settings
    port = settings.web_port
    if port is None:
        _LOG.info("Web server disabled: WEB_PORT (or TOPGG_WEBHOOK_PORT) not set.")
        return None

    app = web.Application(middlewares=[_cors_middleware(settings.web_allowed_origins)])

    from pathlib import Path

    manual_static = (
        Path(__file__).resolve().parent.parent / "static" / "manual_cards"
    )
    if manual_static.is_dir():
        app.router.add_static("/static/manual-cards", manual_static)
        _LOG.info("Manual card images mounted at /static/manual-cards/")

    if settings.topgg_webhook_secret:
        from poke_pon_bot.web.topgg_webhook import register_topgg_routes

        register_topgg_routes(app, bot=bot, settings=settings)

    from poke_pon_bot.web.catalog_api import register_catalog_public_api
    from poke_pon_bot.web.events_api import register_events_public_api
    from poke_pon_bot.web.packs_api import register_packs_public_api

    register_catalog_public_api(app, bot=bot)
    register_packs_public_api(app, bot=bot)
    register_events_public_api(app, bot=bot)

    if (
        settings.web_session_secret
        and settings.discord_oauth_client_id
        and settings.discord_oauth_client_secret
    ):
        from poke_pon_bot.web.auction_api import register_auction_api
        from poke_pon_bot.web.collection_api import register_collection_api
        from poke_pon_bot.web.assembly_api import register_assembly_api
        from poke_pon_bot.web.craft_api import register_craft_api
        from poke_pon_bot.web.deck_api import register_deck_api
        from poke_pon_bot.web.leaderboard_api import register_leaderboard_api
        from poke_pon_bot.web.oauth import register_oauth_routes
        from poke_pon_bot.web.packs_api import register_packs_api
        from poke_pon_bot.web.profile_api import register_profile_api
        from poke_pon_bot.web.shop_api import register_shop_api
        from poke_pon_bot.web.trade_api import register_trade_api
        from poke_pon_bot.web.trade_ws import register_trade_ws
        from poke_pon_bot.web.duel_api import register_duel_api
        from poke_pon_bot.web.duel_ws import register_duel_ws

        register_oauth_routes(
            app, settings=settings, secure_cookies=_secure_cookies(settings), bot=bot
        )
        register_collection_api(app, bot=bot, settings=settings)
        register_craft_api(app, bot=bot, settings=settings)
        register_assembly_api(app, bot=bot, settings=settings)
        register_packs_api(app, bot=bot, settings=settings)
        register_deck_api(app, bot=bot, settings=settings)
        register_auction_api(app, bot=bot, settings=settings)
        register_trade_api(app, bot=bot, settings=settings)
        register_trade_ws(app, bot=bot, settings=settings)
        register_duel_api(app, bot=bot, settings=settings)
        register_duel_ws(app, bot=bot, settings=settings)
        register_profile_api(app, bot=bot, settings=settings)
        register_leaderboard_api(app, bot=bot, settings=settings)
        register_shop_api(app, bot=bot, settings=settings)
        _LOG.info(
            "Discord OAuth + Collection / Deck / Auction / Trade / Profile / "
            "Duel / Leaderboard / Shop API mounted."
        )

    if not list(app.router.routes()):
        _LOG.info("Web server disabled: no routes were enabled by settings.")
        return None

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.web_host, port=int(port))
    await site.start()
    _LOG.info(
        "Web server listening on http://%s:%s — front it with HTTPS (reverse proxy / "
        "Cloudflare Tunnel / ngrok) before pointing the public site at it.",
        settings.web_host,
        port,
    )
    return WebServer(runner=runner, site=site)
