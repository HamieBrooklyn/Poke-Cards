"""Timed auctions: duration parsing, placing bids (wallet escrow), settling expired listings."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.auction import (
    AUCTION_STATUS_ACTIVE,
    AUCTION_STATUS_ENDED_NO_BIDS,
    AUCTION_STATUS_ENDED_SOLD,
    CardAuction,
)
from poke_pon_bot.models.inventory import UserCardInstance
from poke_pon_bot.services.combat_deck import strip_instances_from_deck
from poke_pon_bot.services.instance_public_id import normalize_public_id
from poke_pon_bot.services.wallet import InsufficientPokedollarsError, WalletService, format_pokedollars

MIN_AUCTION_DURATION_MINUTES = 5
MAX_AUCTION_DURATION_MINUTES = 10080  # 7 days

_DURATION_TOKEN_RE = re.compile(r"^\s*(\d+)\s*([mhd])?\s*$", re.IGNORECASE)
_WORD_MINUTES_RE = re.compile(r"^\s*(\d+)\s*(?:minutes?|mins?)\s*$", re.IGNORECASE)
_WORD_HOURS_RE = re.compile(r"^\s*(\d+)\s*(?:hours?|hrs?)\s*$", re.IGNORECASE)
_WORD_DAYS_RE = re.compile(r"^\s*(\d+)\s*(?:days?)\s*$", re.IGNORECASE)


def parse_auction_duration_minutes(raw: str) -> tuple[int | None, str | None]:
    """Parse user duration input into whole minutes, or ``(None, error)``.

    Accepts plain minutes (**120**), suffixes (**90m**, **2h**, **1d**), or words (**30 minutes**, **2 hours**, **1 day**).
    """
    s = (raw or "").strip().replace(",", "")
    if not s:
        return None, "Enter how long the auction should run."
    s_lower = s.lower()
    m = _DURATION_TOKEN_RE.match(s_lower)
    minutes: int | None = None
    if m:
        n = int(m.group(1))
        suf = (m.group(2) or "").lower()
        mult = {"m": 1, "h": 60, "d": 1440}.get(suf, 1)
        minutes = n * mult
    else:
        wm = _WORD_MINUTES_RE.match(s_lower)
        wh = _WORD_HOURS_RE.match(s_lower)
        wd = _WORD_DAYS_RE.match(s_lower)
        if wm:
            minutes = int(wm.group(1))
        elif wh:
            minutes = int(wh.group(1)) * 60
        elif wd:
            minutes = int(wd.group(1)) * 1440
    if minutes is None:
        return (
            None,
            "Examples: **`120`** (minutes), **`90m`** **`2h`** **`1d`**, or **`30 minutes`**, **`2 hours`**, **`1 day`**.",
        )
    if minutes < MIN_AUCTION_DURATION_MINUTES:
        return (
            None,
            f"Auction must run at least **{MIN_AUCTION_DURATION_MINUTES}** minutes.",
        )
    if minutes > MAX_AUCTION_DURATION_MINUTES:
        return None, f"Auction duration cannot exceed **{MAX_AUCTION_DURATION_MINUTES // 1440}** days."
    return minutes, None


async def resolve_auction_id_for_bid(
    session: AsyncSession,
    raw: str,
) -> tuple[int | None, str | None]:
    """Resolve slash/prefix input to an active auction PK: digits = listing id, else Card ``public_id``."""
    s = (raw or "").strip()
    if not s:
        return (
            None,
            "Give the **listing number** from **`/auction search`** (e.g. **`12`**) or paste the listing’s **Card ID**.",
        )
    if s.isdigit():
        aid = int(s)
        if aid < 1:
            return None, "Listing number must be positive."
        auc = await session.get(CardAuction, aid)
        if auc is None or auc.status != AUCTION_STATUS_ACTIVE:
            return None, "No active auction with that listing number."
        return aid, None
    pid = normalize_public_id(s)
    if pid is None:
        return (
            None,
            "Use the **listing number** (digits only, shown at the start of each search line) or the card’s **Card ID**.",
        )
    row = await session.execute(
        select(CardAuction.id)
        .join(UserCardInstance, CardAuction.instance_id == UserCardInstance.id)
        .where(
            UserCardInstance.public_id == pid,
            CardAuction.status == AUCTION_STATUS_ACTIVE,
        )
        .limit(1),
    )
    first = row.first()
    if first is None:
        return None, "There’s no **active** auction for that Card ID."
    return int(first[0]), None


def utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_auction_time_remaining(ends_at: datetime) -> str:
    ends = _ensure_utc(ends_at)
    delta = ends - utc_now()
    sec = int(delta.total_seconds())
    if sec <= 0:
        return "(closing soon)"
    if sec < 3600:
        return f"{sec // 60}m left"
    if sec < 86400:
        return f"{sec // 3600}h {(sec % 3600) // 60}m left"
    return f"{sec // 86400}d {(sec % 86400) // 3600}h left"


async def place_auction_bid(
    session: AsyncSession,
    wallet: WalletService,
    *,
    auction_id: int,
    bidder_discord_id: int,
    amount: int,
    max_bid_amount: int,
) -> str | None:
    """Place a bid; wallets mirror escrow (only highest bidder balance is held). Returns error or ``None``."""
    if amount < 1:
        return "Bid amount must be positive."
    if amount > max_bid_amount:
        return f"Bid cannot exceed **{format_pokedollars(max_bid_amount)}**."

    auc = await session.get(CardAuction, auction_id)
    if auc is None:
        return "That listing was not found."
    if auc.status != AUCTION_STATUS_ACTIVE:
        return "That auction is no longer active."
    if _ensure_utc(auc.ends_at) <= utc_now():
        return "That auction has ended."

    if bidder_discord_id == auc.seller_discord_id:
        return "You can't bid on your own auction."

    if auc.high_bid_pokedollars is not None and int(auc.high_bid_pokedollars) >= max_bid_amount:
        return "That auction is already at the maximum possible bid."

    min_needed = int(auc.price_pokedollars)
    if auc.high_bid_pokedollars is not None:
        min_needed = int(auc.high_bid_pokedollars) + 1
    if amount < min_needed:
        return f"Bid must be at least **{format_pokedollars(min_needed)}**."

    prev_bidder = auc.high_bidder_discord_id
    prev_amt = auc.high_bid_pokedollars

    try:
        await wallet.try_debit(session, bidder_discord_id, amount)
    except InsufficientPokedollarsError:
        return "You don't have enough **Pokedollars** for that bid."

    if prev_bidder is not None and prev_amt is not None and prev_amt > 0:
        await wallet.try_credit(session, prev_bidder, prev_amt)

    auc.high_bidder_discord_id = bidder_discord_id
    auc.high_bid_pokedollars = amount
    return None


async def _settle_one(session: AsyncSession, wallet: WalletService, auc: CardAuction) -> None:
    inst = await session.get(UserCardInstance, auc.instance_id)
    seller_id = auc.seller_discord_id

    if inst is None:
        auc.status = AUCTION_STATUS_ENDED_NO_BIDS
        auc.high_bidder_discord_id = None
        auc.high_bid_pokedollars = None
        return

    winner_id = auc.high_bidder_discord_id
    winning_bid = auc.high_bid_pokedollars

    if winner_id is None or winning_bid is None or winning_bid <= 0:
        auc.status = AUCTION_STATUS_ENDED_NO_BIDS
        return

    await strip_instances_from_deck(session, seller_id, {inst.id})
    inst.discord_user_id = winner_id
    await wallet.try_credit(session, seller_id, winning_bid)
    auc.status = AUCTION_STATUS_ENDED_SOLD


async def settle_due_auctions(
    async_session_factory: async_sessionmaker[AsyncSession],
    wallet: WalletService,
) -> int:
    """Close expired **active** auctions (sold or no bids). Returns how many were settled."""
    settled = 0
    while True:
        async with async_session_factory() as session:
            row = await session.execute(
                select(CardAuction)
                .where(
                    CardAuction.status == AUCTION_STATUS_ACTIVE,
                    CardAuction.ends_at <= utc_now(),
                )
                .limit(1),
            )
            auc = row.scalar_one_or_none()
            if auc is None:
                break
            await _settle_one(session, wallet, auc)
            await session.commit()
            settled += 1
    return settled
