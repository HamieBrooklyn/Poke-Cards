"""Stripe Checkout + webhook fulfillment for the web shop."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.stripe_purchase import StripePurchase
from poke_pon_bot.services.crystals import CrystalsService
from poke_pon_bot.services.shop_catalog import (
    HALF_DROP_COOLDOWN_SKU_ID,
    ShopSkuDef,
    sku_by_id,
)
from poke_pon_bot.services.wallet import WalletService

_LOG = logging.getLogger(__name__)

_PRICE_CACHE_TTL_SECONDS = 300.0
_price_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def format_stripe_unit_amount(unit_amount: int, currency: str) -> str:
    """Human-readable price from Stripe's minor-unit amount."""
    cur = (currency or "eur").lower()
    major = unit_amount / 100
    if cur == "eur":
        if major == int(major):
            return f"€{int(major)}"
        return f"€{major:.2f}"
    if cur == "usd":
        if major == int(major):
            return f"${int(major)}"
        return f"${major:.2f}"
    if cur == "gbp":
        if major == int(major):
            return f"£{int(major)}"
        return f"£{major:.2f}"
    return f"{major:.2f} {cur.upper()}"


def _retrieve_price_sync(stripe_secret_key: str, price_id: str) -> dict[str, Any] | None:
    stripe.api_key = stripe_secret_key
    price = stripe.Price.retrieve(price_id)
    unit_amount = getattr(price, "unit_amount", None)
    if unit_amount is None:
        return None
    currency = str(getattr(price, "currency", "eur") or "eur")
    return {
        "amount_cents": int(unit_amount),
        "currency": currency,
        "display": format_stripe_unit_amount(int(unit_amount), currency),
    }


async def lookup_stripe_price(stripe_secret_key: str, price_id: str) -> dict[str, Any] | None:
    """Fetch Stripe Price metadata for catalog display (cached briefly)."""
    now = time.monotonic()
    cached = _price_cache.get(price_id)
    if cached is not None and now - cached[0] < _PRICE_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        result = await asyncio.to_thread(_retrieve_price_sync, stripe_secret_key, price_id)
    except Exception:
        _LOG.debug("stripe price lookup failed price_id=%s", price_id, exc_info=True)
        result = None

    _price_cache[price_id] = (now, result)
    return result


async def user_has_stripe_sku(
    session: AsyncSession,
    *,
    discord_user_id: int,
    sku_id: str,
) -> bool:
    """True if this user already has a fulfilled Stripe purchase for ``sku_id``."""
    row = await session.scalar(
        select(StripePurchase.checkout_session_id)
        .where(
            StripePurchase.discord_user_id == discord_user_id,
            StripePurchase.sku_id == sku_id,
        )
        .limit(1)
    )
    return row is not None


async def user_has_stripe_drop_boost(session: AsyncSession, *, discord_user_id: int) -> bool:
    return await user_has_stripe_sku(
        session, discord_user_id=discord_user_id, sku_id=HALF_DROP_COOLDOWN_SKU_ID
    )


@dataclass(frozen=True)
class FulfillResult:
    kind: Literal["paid", "duplicate", "invalid", "already_owned"]
    sku_id: str | None = None
    amount_granted: int = 0
    pack_public_id: str | None = None


async def _grant_random_pack(
    session: AsyncSession,
    *,
    discord_user_id: int,
    bot: Any | None = None,
) -> str:
    from poke_pon_bot.models.card_series import CardSeries
    from poke_pon_bot.services.drops import DropService
    from poke_pon_bot.services.packs import PackService

    ds = DropService()
    ps = PackService()
    pack = await ps.grant_consumable_pack(
        session,
        ds,
        discord_user_id=discord_user_id,
        guild_id=None,
    )
    pack_public_id = pack.public_id

    if bot is not None:
        try:
            import discord

            user = bot.get_user(discord_user_id) or await bot.fetch_user(discord_user_id)
            series = await session.get(CardSeries, pack.series_id)
            series_name = series.display_name if series is not None else "random"
            await user.send(
                f"Thanks for your purchase! You got a **{series_name}** pack `{pack_public_id}` — "
                "open it with `/packv` or `/packcolv`.",
            )
        except Exception:
            _LOG.info("Stripe random-pack DM blocked for user %s.", discord_user_id, exc_info=True)

    return pack_public_id


async def fulfill_checkout_session(
    session: AsyncSession,
    *,
    checkout_session_id: str,
    discord_user_id: int,
    sku_id: str,
    stripe_price_id: str | None,
    wallet: WalletService | None = None,
    crystals: CrystalsService | None = None,
    bot: Any | None = None,
) -> FulfillResult:
    """Credit the player once per Checkout Session id."""
    existing = await session.get(StripePurchase, checkout_session_id)
    if existing is not None:
        return FulfillResult(kind="duplicate", sku_id=existing.sku_id, amount_granted=existing.amount_granted)

    sku = sku_by_id(sku_id)
    if sku is None:
        _LOG.warning("stripe fulfill unknown sku=%s session=%s", sku_id, checkout_session_id)
        return FulfillResult(kind="invalid")

    wallet = wallet or WalletService()
    crystals = crystals or CrystalsService()

    if sku.one_time and await user_has_stripe_sku(session, discord_user_id=discord_user_id, sku_id=sku.id):
        _LOG.warning(
            "stripe fulfill one-time sku already owned uid=%s sku=%s session=%s",
            discord_user_id,
            sku_id,
            checkout_session_id,
        )
        return FulfillResult(kind="already_owned", sku_id=sku.id)

    amount_granted = 0
    pack_public_id: str | None = None
    record_currency = sku.kind

    if sku.kind == "currency":
        if sku.currency == "pokedollars":
            await wallet.try_credit(session, discord_user_id, sku.grant_amount)
        else:
            await crystals.try_credit(session, discord_user_id, sku.grant_amount)
        amount_granted = sku.grant_amount
        record_currency = sku.currency or "currency"
    elif sku.kind == "drop_boost":
        amount_granted = 1
    elif sku.kind == "random_pack":
        pack_public_id = await _grant_random_pack(session, discord_user_id=discord_user_id, bot=bot)
        amount_granted = 1
    else:
        _LOG.warning("stripe fulfill unsupported kind=%s sku=%s", sku.kind, sku_id)
        return FulfillResult(kind="invalid")

    session.add(
        StripePurchase(
            checkout_session_id=checkout_session_id,
            discord_user_id=discord_user_id,
            sku_id=sku.id,
            currency=record_currency,
            amount_granted=amount_granted,
            stripe_price_id=stripe_price_id,
            processed_at=datetime.now(UTC),
        )
    )
    return FulfillResult(
        kind="paid",
        sku_id=sku.id,
        amount_granted=amount_granted,
        pack_public_id=pack_public_id,
    )


def create_checkout_session(
    *,
    stripe_secret_key: str,
    price_id: str,
    sku: ShopSkuDef,
    discord_user_id: int,
    success_url: str,
    cancel_url: str,
) -> stripe.checkout.Session:
    stripe.api_key = stripe_secret_key
    metadata: dict[str, str] = {
        "discord_user_id": str(discord_user_id),
        "sku_id": sku.id,
        "kind": sku.kind,
    }
    if sku.kind == "currency" and sku.currency is not None:
        metadata["currency"] = sku.currency
        metadata["grant_amount"] = str(sku.grant_amount)
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(discord_user_id),
        metadata=metadata,
    )


def construct_webhook_event(
    payload: bytes,
    sig_header: str | None,
    webhook_secret: str,
) -> Any:
    if not sig_header:
        msg = "missing Stripe-Signature header"
        raise ValueError(msg)
    return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
