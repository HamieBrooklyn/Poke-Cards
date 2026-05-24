"""Stripe shop API: catalog, Checkout Session, webhook fulfillment."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from poke_pon_bot.services.crystals import CrystalsService, format_crystals
from poke_pon_bot.services.shop_catalog import (
    SHOP_SKU_DEFS,
    currency_skus,
    perk_skus,
    sku_by_id,
)
from poke_pon_bot.services.stripe_shop import (
    construct_webhook_event,
    create_checkout_session,
    fulfill_checkout_session,
    lookup_stripe_price,
    user_has_stripe_sku,
)
from poke_pon_bot.services.wallet import WalletService, format_pokedollars
from poke_pon_bot.web.frontend_urls import shop_page_url
from poke_pon_bot.web.sessions import read_session

_LOG = logging.getLogger(__name__)


def _shop_base_url(settings: Any) -> str:
    """Canonical shop page on GitHub Pages (never append /shop/ to a file path)."""
    return shop_page_url(settings)


def _grant_label_for(sku_def: Any) -> str:
    if sku_def.kind == "drop_boost":
        return "Permanent perk"
    if sku_def.kind == "random_pack":
        return "1 random pack"
    if sku_def.currency == "pokedollars":
        return format_pokedollars(sku_def.grant_amount)
    return format_crystals(sku_def.grant_amount)


def _serialize_catalog_item(
    sku_def: Any,
    price_id: str | None,
    *,
    stripe_price: dict[str, Any] | None = None,
    owned: bool = False,
) -> dict[str, Any]:
    available = bool(price_id)
    if sku_def.one_time and owned:
        available = False
    payload: dict[str, Any] = {
        "id": sku_def.id,
        "kind": sku_def.kind,
        "currency": sku_def.currency,
        "grant_amount": sku_def.grant_amount,
        "grant_label": _grant_label_for(sku_def),
        "title": sku_def.title,
        "description": sku_def.description,
        "badge": sku_def.badge,
        "one_time": sku_def.one_time,
        "owned": owned,
        "available": available,
    }
    if stripe_price:
        payload["price"] = stripe_price
    return payload


def register_shop_api(app: web.Application, *, bot: Any, settings: Any) -> None:
    if not settings.web_session_secret:
        return
    if not settings.stripe_secret_key:
        _LOG.info("Shop API disabled: STRIPE_SECRET_KEY not set.")
        return

    session_factory = bot.async_session_factory
    session_secret = settings.web_session_secret
    session_ttl = settings.web_session_ttl_seconds
    stripe_secret = settings.stripe_secret_key
    stripe_webhook_secret = settings.stripe_webhook_secret
    price_ids: dict[str, str] = settings.stripe_price_ids
    wallet = WalletService()
    crystals = CrystalsService()

    def _require_session(request: web.Request):
        sess = read_session(request, session_secret, max_age=session_ttl)
        if sess is None:
            raise web.HTTPUnauthorized(
                text='{"error":"unauthenticated"}',
                content_type="application/json",
            )
        return sess

    async def handle_catalog(request: web.Request) -> web.StreamResponse:
        sess = read_session(request, session_secret, max_age=session_ttl)
        pokedollars: list[dict[str, Any]] = []
        crystal_items: list[dict[str, Any]] = []
        perks: list[dict[str, Any]] = []

        async def _price_for(sku_id: str) -> dict[str, Any] | None:
            pid = price_ids.get(sku_id)
            if not pid:
                return None
            return await lookup_stripe_price(stripe_secret, pid)

        stripe_prices = await asyncio.gather(*[_price_for(s.id) for s in SHOP_SKU_DEFS])
        price_by_sku = {s.id: stripe_prices[i] for i, s in enumerate(SHOP_SKU_DEFS)}

        owned_skus: set[str] = set()
        if sess is not None:
            try:
                async with session_factory() as db:
                    for perk in perk_skus():
                        if perk.one_time and await user_has_stripe_sku(
                            db, discord_user_id=int(sess.user_id), sku_id=perk.id
                        ):
                            owned_skus.add(perk.id)
            except SQLAlchemyError:
                _LOG.exception("shop catalog owned perks uid=%s", sess.user_id)

        for sku_def in currency_skus():
            pid = price_ids.get(sku_def.id)
            item = _serialize_catalog_item(
                sku_def,
                pid,
                stripe_price=price_by_sku.get(sku_def.id),
            )
            if sku_def.currency == "pokedollars":
                pokedollars.append(item)
            else:
                crystal_items.append(item)

        for sku_def in perk_skus():
            pid = price_ids.get(sku_def.id)
            perks.append(
                _serialize_catalog_item(
                    sku_def,
                    pid,
                    stripe_price=price_by_sku.get(sku_def.id),
                    owned=sku_def.id in owned_skus,
                )
            )

        balances = None
        if sess is not None:
            try:
                async with session_factory() as db:
                    pd = await wallet.get_balance(db, int(sess.user_id))
                    cr = await crystals.get_balance(db, int(sess.user_id))
                balances = {"pokedollars": pd, "crystals": cr}
            except SQLAlchemyError:
                _LOG.exception("shop catalog balances uid=%s", sess.user_id)

        return web.json_response(
            {
                "enabled": bool(price_ids),
                "pokedollars": pokedollars,
                "crystals": crystal_items,
                "perks": perks,
                "balances": balances,
            }
        )

    async def handle_checkout(request: web.Request) -> web.StreamResponse:
        session = _require_session(request)
        uid = int(session.user_id)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid_json"}, status=400)

        sku_id = (body.get("sku_id") or body.get("sku") or "").strip()
        sku = sku_by_id(sku_id)
        if sku is None:
            return web.json_response({"error": "unknown_sku"}, status=400)

        price_id = price_ids.get(sku_id)
        if not price_id:
            return web.json_response({"error": "sku_not_configured"}, status=503)

        if sku.one_time:
            try:
                async with session_factory() as db:
                    if await user_has_stripe_sku(db, discord_user_id=uid, sku_id=sku_id):
                        return web.json_response(
                            {
                                "error": "already_purchased",
                                "message": "You already own this perk on your account.",
                            },
                            status=409,
                        )
            except SQLAlchemyError:
                _LOG.exception("shop checkout owned check uid=%s sku=%s", uid, sku_id)
                return web.json_response({"error": "database_error"}, status=500)

        shop_url = _shop_base_url(settings)
        success_qs = urlencode({"success": "1", "sku": sku_id})
        cancel_qs = urlencode({"cancel": "1"})
        success_url = f"{shop_url}?{success_qs}"
        cancel_url = f"{shop_url}?{cancel_qs}"

        try:
            checkout = await asyncio.to_thread(
                create_checkout_session,
                stripe_secret_key=stripe_secret,
                price_id=price_id,
                sku=sku,
                discord_user_id=uid,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except Exception as exc:
            _LOG.exception("stripe checkout create sku=%s uid=%s", sku_id, uid)
            message = str(exc)
            user_message = getattr(exc, "user_message", None)
            if user_message:
                message = str(user_message)
            return web.json_response(
                {"error": "stripe_error", "message": message},
                status=502,
            )

        url = checkout.url
        if not url:
            return web.json_response({"error": "stripe_no_url"}, status=502)

        return web.json_response({"checkout_url": url, "session_id": checkout.id})

    async def handle_stripe_webhook(request: web.Request) -> web.StreamResponse:
        if not stripe_webhook_secret:
            _LOG.warning("Stripe webhook received but STRIPE_WEBHOOK_SECRET unset")
            return web.Response(status=503, text="webhook not configured")

        payload = await request.read()
        sig = request.headers.get("Stripe-Signature")

        try:
            event = construct_webhook_event(payload, sig, stripe_webhook_secret)
        except ValueError:
            _LOG.warning("Stripe webhook signature invalid")
            return web.Response(status=400, text="invalid signature")
        except Exception:
            _LOG.exception("Stripe webhook parse failed")
            return web.Response(status=400, text="invalid payload")

        if event.type != "checkout.session.completed":
            return web.Response(status=200, text="ignored")

        cs = event.data.object
        session_id = getattr(cs, "id", None)
        metadata = getattr(cs, "metadata", None) or {}
        sku_id = (metadata.get("sku_id") or "").strip()
        user_raw = (metadata.get("discord_user_id") or getattr(cs, "client_reference_id", "") or "").strip()

        if not session_id or not sku_id or not user_raw.isdigit():
            _LOG.warning("Stripe webhook incomplete metadata session=%s", session_id)
            return web.Response(status=200, text="incomplete metadata")

        discord_user_id = int(user_raw)
        price_id = None
        try:
            line_items = getattr(cs, "line_items", None)
            if line_items and line_items.data:
                price_id = line_items.data[0].price.id
        except Exception:
            price_id = None

        try:
            async with session_factory() as db:
                result = await fulfill_checkout_session(
                    db,
                    checkout_session_id=str(session_id),
                    discord_user_id=discord_user_id,
                    sku_id=sku_id,
                    stripe_price_id=price_id,
                    wallet=wallet,
                    crystals=crystals,
                    bot=bot,
                )
                await db.commit()
        except SQLAlchemyError:
            _LOG.exception("stripe fulfill db error session=%s", session_id)
            return web.Response(status=500, text="database error")

        if result.kind == "paid":
            _LOG.info(
                "Stripe purchase fulfilled uid=%s sku=%s amount=%s pack=%s session=%s",
                discord_user_id,
                result.sku_id,
                result.amount_granted,
                result.pack_public_id,
                session_id,
            )
        return web.Response(status=200, text="ok")

    app.router.add_get("/api/shop/catalog", handle_catalog)
    app.router.add_post("/api/shop/checkout", handle_checkout)
    app.router.add_post("/api/stripe/webhook", handle_stripe_webhook)
