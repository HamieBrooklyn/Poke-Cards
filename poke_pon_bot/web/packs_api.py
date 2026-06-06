"""Pack catalog, pull odds, and owned unopened packs (``/packcolv``)."""



from __future__ import annotations



import logging

from typing import Any



from aiohttp import web

from sqlalchemy.exc import SQLAlchemyError



from poke_pon_bot.models.card_series import CardSeries

from poke_pon_bot.services.pack_odds import build_series_pull_odds, search_global_pack_pool

from poke_pon_bot.services.packs import PackService

from poke_pon_bot.web.sessions import read_session



_LOG = logging.getLogger(__name__)





def _utc_iso(dt: Any) -> str | None:

    if dt is None:

        return None

    return dt.isoformat()





def register_packs_public_api(app: web.Application, *, bot: Any) -> None:

    """Public read-only pack pool routes (no Discord session required)."""

    session_factory = bot.async_session_factory



    async def handle_catalog_list(request: web.Request) -> web.StreamResponse:

        q = (request.rel_url.query.get("q") or "").strip().lower()

        try:

            page = max(1, int(request.rel_url.query.get("page") or "1"))

        except ValueError:

            page = 1

        try:

            page_size = min(80, max(1, int(request.rel_url.query.get("page_size") or "40")))

        except ValueError:

            page_size = 40



        ps = PackService()

        try:

            async with session_factory() as db:

                series_list = await ps.list_active_series(db)

        except SQLAlchemyError:

            _LOG.exception("packs_api catalog list")

            return web.json_response({"error": "database_error"}, status=500)



        items: list[dict[str, Any]] = []

        for s in series_list:

            if q:
                haystack = (
                    (s.display_name or "").lower()
                    + " " + (s.code or "").lower()
                    + " " + (s.description or "").lower()
                )
                if q not in haystack:
                    continue

            items.append(

                {

                    "code": s.code,

                    "display_name": s.display_name,

                    "description": s.description,

                    "crystal_price": int(s.crystal_price),

                    "pack_art_url": s.pack_art_url,

                    "cards_per_pack": int(s.cards_per_pack),

                    "code_cards_per_pack": int(s.code_cards_per_pack),

                }

            )

        items.sort(key=lambda x: (x["display_name"] or x["code"] or "").lower())

        total = len(items)

        start = (page - 1) * page_size

        page_items = items[start : start + page_size]



        return web.json_response(

            {

                "total": total,

                "page": page,

                "page_size": page_size,

                "items": page_items,

            }

        )



    async def handle_pack_search(request: web.Request) -> web.StreamResponse:

        q = (request.rel_url.query.get("q") or "").strip()

        sort = (request.rel_url.query.get("sort") or "top").strip().lower()

        if sort not in ("top", "rarity", "cost_high", "cost_low"):

            sort = "top"

        try:

            page = max(1, int(request.rel_url.query.get("page") or "1"))

        except ValueError:

            page = 1

        try:

            page_size = min(120, max(1, int(request.rel_url.query.get("page_size") or "60")))

        except ValueError:

            page_size = 60



        pack_code = (request.rel_url.query.get("pack") or "").strip()

        try:

            async with session_factory() as db:

                payload = await search_global_pack_pool(

                    db,

                    q=q,

                    sort=sort,

                    pack_code=pack_code,

                    page=page,

                    page_size=page_size,

                )

        except LookupError as exc:

            return web.json_response({"error": str(exc)}, status=500)

        except SQLAlchemyError:

            _LOG.exception("packs_api search")

            return web.json_response({"error": "database_error"}, status=500)



        return web.json_response(payload)



    async def handle_catalog_detail(request: web.Request) -> web.StreamResponse:

        code = (request.match_info.get("code") or "").strip()

        if not code:

            raise web.HTTPBadRequest(text='{"error":"missing_code"}', content_type="application/json")



        card_q = (request.rel_url.query.get("card_q") or "").strip().lower()

        try:

            card_page = max(1, int(request.rel_url.query.get("card_page") or "1"))

        except ValueError:

            card_page = 1

        try:

            card_page_size = min(

                200, max(1, int(request.rel_url.query.get("card_page_size") or "60"))

            )

        except ValueError:

            card_page_size = 60



        ps = PackService()

        try:

            async with session_factory() as db:

                series = await ps.get_series_by_code(db, code)

                if series is None or not series.is_active:

                    return web.json_response({"error": "not_found"}, status=404)

                payload = await build_series_pull_odds(db, series)

        except LookupError as exc:

            return web.json_response({"error": str(exc)}, status=500)

        except SQLAlchemyError:

            _LOG.exception("packs_api catalog detail code=%s", code)

            return web.json_response({"error": "database_error"}, status=500)



        cards = payload.get("cards") or []

        if card_q:

            cards = [

                c

                for c in cards

                if card_q in (c.get("name") or "").lower()

                or card_q in (c.get("set_code") or "").lower()

            ]

        total_cards = len(cards)

        start = (card_page - 1) * card_page_size

        payload["cards"] = cards[start : start + card_page_size]

        payload["cards_total"] = total_cards

        payload["cards_page"] = card_page

        payload["cards_page_size"] = card_page_size



        return web.json_response(payload)



    app.router.add_get("/api/packs/search", handle_pack_search)

    app.router.add_get("/api/packs/catalog", handle_catalog_list)

    app.router.add_get("/api/packs/catalog/{code}", handle_catalog_detail)

    _LOG.info(

        "Packs public API mounted: GET /api/packs/search, /api/packs/catalog, "

        "/api/packs/catalog/{code}"

    )





def register_packs_api(app: web.Application, *, bot: Any, settings: Any) -> None:

    """Signed-in user's unopened packs."""

    if not settings.web_session_secret:

        return



    register_packs_public_api(app, bot=bot)



    session_factory = bot.async_session_factory

    session_secret = settings.web_session_secret

    session_ttl = settings.web_session_ttl_seconds



    def _require_session(request: web.Request):

        sess = read_session(request, session_secret, max_age=session_ttl)

        if sess is None:

            raise web.HTTPUnauthorized(

                text='{"error":"unauthenticated"}',

                content_type="application/json",

            )

        return sess



    async def handle_list_packs(request: web.Request) -> web.StreamResponse:

        session = _require_session(request)

        ps = PackService()

        try:

            async with session_factory() as db:

                packs = await ps.list_unopened_for_user(

                    db, discord_user_id=session.user_id

                )

                items: list[dict[str, Any]] = []

                for pack in packs:

                    series = await db.get(CardSeries, pack.series_id)

                    items.append(

                        {

                            "public_id": pack.public_id,

                            "source": pack.source,

                            "obtained_at": _utc_iso(pack.obtained_at),

                            "series": {

                                "code": series.code if series else None,

                                "display_name": (

                                    series.display_name if series else None

                                ),

                                "pack_art_url": (

                                    series.pack_art_url if series else None

                                ),

                            },

                        }

                    )

        except SQLAlchemyError:

            _LOG.exception("packs_api list user=%s", session.user_id)

            return web.json_response({"error": "database_error"}, status=500)



        return web.json_response({"total": len(items), "items": items})



    app.router.add_get("/api/me/packs", handle_list_packs)

    _LOG.info("Packs user API mounted: GET /api/me/packs")


