"""Incremental catalog sync + website/Discord announcements for new TCG content."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.catalog_announcement import (
    ANNOUNCE_KIND_CARDS_ADDED,
    ANNOUNCE_KIND_NEW_SET,
    CatalogAnnouncement,
)
from poke_pon_bot.models.rarity import RarityClass
from poke_pon_bot.services.catalog_sync import (
    SetSyncStats,
    fetch_sets_released_since,
    sync_curated_sets,
)
from poke_pon_bot.services.pack_series_loader import sync_pack_series_from_catalog

_LOG = logging.getLogger(__name__)

TEST_SET_CODE_PREFIX = "dev-seed"
FEATURED_CARD_LIMIT = 5


@dataclass
class CatalogNewsRunResult:
    sets_checked: int = 0
    sets_synced: int = 0
    announcements_created: int = 0
    discord_posts: int = 0
    messages: list[str] = field(default_factory=list)


def _site_origin(frontend_url: str | None) -> str:
    raw = (frontend_url or "https://pokepon.org/collection/").strip()
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://pokepon.org"


def _pokedex_url(origin: str, *, catalog_set_code: str, announcement_id: int) -> str:
    code = catalog_set_code.strip()
    return (
        f"{origin.rstrip('/')}/pokedex/#set_code={code}&news_id={int(announcement_id)}"
    )


def _packs_url(origin: str, set_code: str) -> str:
    return f"{origin.rstrip('/')}/packs/?pack={set_code.strip()}"


def _parse_card_ids_json(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[int] = []
    for item in parsed:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _catalog_set_code_for_row(row: CatalogAnnouncement) -> str:
    return (row.catalog_set_code or row.set_code or "").strip()


def _serialize_announcement(row: CatalogAnnouncement) -> dict[str, Any]:
    try:
        samples = json.loads(row.sample_cards_json or "[]")
    except json.JSONDecodeError:
        samples = []
    card_ids = _parse_card_ids_json(row.card_ids_json)
    catalog_set_code = _catalog_set_code_for_row(row)
    return {
        "id": int(row.id),
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "set_code": row.set_code,
        "catalog_set_code": catalog_set_code,
        "set_name": row.set_name,
        "new_card_count": int(row.new_card_count),
        "card_ids": card_ids,
        "sample_cards": samples,
        "pokedex_url": row.pokedex_url,
        "packs_url": row.packs_url,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


def _title_for_delta(stats: SetSyncStats) -> str:
    name = stats.set_name or stats.set_code
    if stats.is_new_set:
        return f"New set: {name}"
    return f"New cards in {name}"


def _body_for_delta(stats: SetSyncStats) -> str:
    n = stats.new_cards
    name = stats.set_name or stats.set_code
    if stats.is_new_set:
        return (
            f"**{n}** cards from **{name}** (`{stats.set_code}`) are now in the catalog — "
            "hunt them in `/cd`, `/packv`, and the Pokédex."
        )
    return (
        f"**{n}** new printings were added to **{name}** (`{stats.set_code}`)."
    )


async def _should_announce(
    session: AsyncSession,
    stats: SetSyncStats,
    *,
    min_cards_existing_set: int,
) -> str | None:
    if stats.new_cards <= 0:
        return None
    if stats.is_new_set:
        return ANNOUNCE_KIND_NEW_SET
    if stats.new_cards < min_cards_existing_set:
        return None
    existing = await session.scalar(
        select(CatalogAnnouncement.id).where(
            CatalogAnnouncement.set_code == stats.set_code,
            CatalogAnnouncement.kind == ANNOUNCE_KIND_CARDS_ADDED,
            CatalogAnnouncement.published_at
            >= datetime.now(UTC) - timedelta(days=7),
        )
    )
    if existing is not None:
        return None
    return ANNOUNCE_KIND_CARDS_ADDED


async def _create_announcement(
    session: AsyncSession,
    *,
    kind: str,
    stats: SetSyncStats,
    site_origin: str,
) -> CatalogAnnouncement | None:
    if kind == ANNOUNCE_KIND_NEW_SET:
        dup = await session.scalar(
            select(CatalogAnnouncement.id).where(
                CatalogAnnouncement.kind == ANNOUNCE_KIND_NEW_SET,
                CatalogAnnouncement.set_code == stats.set_code,
            )
        )
        if dup is not None:
            return None

    samples = [
        {k: v for k, v in s.items() if k != "rarity_sort_order"}
        for s in stats.sample_cards[:FEATURED_CARD_LIMIT]
    ]
    catalog_set_code = stats.set_code
    row = CatalogAnnouncement(
        kind=kind,
        set_code=stats.set_code,
        catalog_set_code=catalog_set_code,
        set_name=stats.set_name or stats.set_code,
        title=_title_for_delta(stats),
        body=_body_for_delta(stats),
        new_card_count=int(stats.new_cards),
        sample_cards_json=json.dumps(samples, ensure_ascii=False),
        card_ids_json=json.dumps(stats.new_card_ids, ensure_ascii=False),
        packs_url=_packs_url(site_origin, catalog_set_code),
    )
    session.add(row)
    await session.flush()
    row.pokedex_url = _pokedex_url(
        site_origin,
        catalog_set_code=catalog_set_code,
        announcement_id=int(row.id),
    )
    return row


async def get_news_public(
    session: AsyncSession,
    *,
    announcement_id: int,
) -> dict[str, Any] | None:
    row = await session.get(CatalogAnnouncement, int(announcement_id))
    if row is None:
        return None
    return _serialize_announcement(row)


async def list_news_public(
    session: AsyncSession,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(50, int(limit)))
    rows = (
        await session.execute(
            select(CatalogAnnouncement)
            .order_by(CatalogAnnouncement.published_at.desc())
            .limit(limit)
        )
    ).scalars()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(_serialize_announcement(row))
    return out


@dataclass
class CatalogNewsSeedResult:
    announcement_id: int
    title: str
    set_code: str
    discord_posted: bool
    cleared_previous: int = 0


async def _cards_for_seed(
    session: AsyncSession,
    *,
    set_code: str,
    card_limit: int = 12,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Card.id,
            Card.name,
            Card.image_small_url,
            RarityClass.sort_order,
        )
        .join(RarityClass, Card.rarity_class_id == RarityClass.id, isouter=True)
        .where(Card.set_code == set_code, Card.image_small_url != "")
        .order_by(RarityClass.sort_order.desc().nullslast(), Card.collector_number.asc())
        .limit(max(1, card_limit))
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": int(card_id),
            "name": name,
            "image_small_url": image_small_url,
        }
        for card_id, name, image_small_url, _sort in rows
        if image_small_url
    ]


async def delete_test_catalog_announcements(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Remove prior dev-seed rows (staging test cleanup)."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(CatalogAnnouncement).where(
                    CatalogAnnouncement.set_code.like(f"{TEST_SET_CODE_PREFIX}%")
                )
            )
        ).scalars()
        deleted = 0
        for row in rows:
            await session.delete(row)
            deleted += 1
        if deleted:
            await session.commit()
        return deleted


async def seed_test_catalog_announcement(
    bot: discord.Client,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    site_origin: str,
    kind: str = ANNOUNCE_KIND_NEW_SET,
    set_code: str | None = None,
    post_discord: bool = False,
    discord_channel_id: int | None = None,
    clear_previous: bool = False,
) -> CatalogNewsSeedResult:
    """Insert a [TEST] catalog news row for website/Discord smoke tests."""
    if kind not in (ANNOUNCE_KIND_NEW_SET, ANNOUNCE_KIND_CARDS_ADDED):
        raise ValueError(f"unsupported kind: {kind}")

    cleared = 0
    if clear_previous:
        cleared = await delete_test_catalog_announcements(session_factory)

    link_set_code = (set_code or "sv2").strip().lower()
    stamp = int(datetime.now(UTC).timestamp())
    row_set_code = f"{TEST_SET_CODE_PREFIX}-{stamp}"

    async with session_factory() as session:
        set_name = link_set_code
        card_row = await session.scalar(
            select(Card.set_name)
            .where(Card.set_code == link_set_code)
            .limit(1)
        )
        if card_row:
            set_name = str(card_row)

        featured = await _cards_for_seed(session, set_code=link_set_code, card_limit=12)
        if not featured:
            featured = await _cards_for_seed(session, set_code="sv2", card_limit=12)
        card_ids = [int(c["id"]) for c in featured]
        samples = featured[:FEATURED_CARD_LIMIT]

        if kind == ANNOUNCE_KIND_NEW_SET:
            title = f"[TEST] New set: {set_name}"
            body = (
                f"**Staging test** — **{len(card_ids) or 42}** cards from **{set_name}** "
                f"(`{link_set_code}`) would appear here after a real catalog sync."
            )
            new_card_count = len(card_ids) or 42
        else:
            title = f"[TEST] New cards in {set_name}"
            body = (
                f"**Staging test** — **{len(card_ids) or 12}** new printings were added to "
                f"**{set_name}** (`{link_set_code}`)."
            )
            new_card_count = len(card_ids) or 12

        row = CatalogAnnouncement(
            kind=kind,
            set_code=row_set_code,
            catalog_set_code=link_set_code,
            set_name=set_name,
            title=title,
            body=body,
            new_card_count=new_card_count,
            sample_cards_json=json.dumps(samples, ensure_ascii=False),
            card_ids_json=json.dumps(card_ids, ensure_ascii=False),
            packs_url=_packs_url(site_origin, link_set_code),
        )
        session.add(row)
        await session.flush()
        row.pokedex_url = _pokedex_url(
            site_origin,
            catalog_set_code=link_set_code,
            announcement_id=int(row.id),
        )
        await session.commit()
        await session.refresh(row)
        announcement_id = int(row.id)
        title_out = str(row.title)
        discord_posted = False

        if post_discord and discord_channel_id is not None:
            msg_id = await post_discord_announcement(
                bot,
                channel_id=int(discord_channel_id),
                row=row,
            )
            if msg_id is not None:
                row.discord_message_id = msg_id
                row.discord_channel_id = int(discord_channel_id)
                await session.commit()
                discord_posted = True

    return CatalogNewsSeedResult(
        announcement_id=announcement_id,
        title=title_out,
        set_code=row_set_code,
        discord_posted=discord_posted,
        cleared_previous=cleared,
    )


async def post_discord_announcement(
    bot: discord.Client,
    *,
    channel_id: int,
    row: CatalogAnnouncement,
) -> int | None:
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.HTTPException):
            _LOG.warning("Catalog news channel %s not found", channel_id)
            return None
    if not isinstance(channel, discord.TextChannel):
        return None

    embed = discord.Embed(
        title=row.title,
        description=row.body or "",
        colour=discord.Colour.green(),
    )
    catalog_set_code = _catalog_set_code_for_row(row)

    try:
        samples = json.loads(row.sample_cards_json or "[]")
    except json.JSONDecodeError:
        samples = []

    featured_samples: list[dict[str, Any]] = []
    for sample in samples[:FEATURED_CARD_LIMIT]:
        if not isinstance(sample, dict):
            continue
        image_url = sample.get("image_small_url") or sample.get("image_large_url")
        if image_url:
            featured_samples.append(sample)

    remaining = int(row.new_card_count) - len(featured_samples)
    if remaining > 0 and row.pokedex_url:
        embed.description = (
            f"{row.body or ''}\n\n+**{remaining}** more → "
            f"[Pokédex]({row.pokedex_url})"
        ).strip()

    embed.add_field(
        name="Browse",
        value=(
            f"[Pokédex — new cards]({row.pokedex_url}) · [Packs]({row.packs_url})"
            if row.pokedex_url and row.packs_url
            else "See pokepon.org"
        ),
        inline=False,
    )
    embed.set_footer(text=f"{row.new_card_count} new cards · {catalog_set_code}")

    embeds: list[discord.Embed] = [embed]
    for sample in featured_samples:
        name = str(sample.get("name") or "New card")
        image_url = sample.get("image_small_url") or sample.get("image_large_url")
        if not image_url:
            continue
        card_embed = discord.Embed(title=name, colour=discord.Colour.dark_green())
        card_embed.set_image(url=str(image_url))
        embeds.append(card_embed)

    try:
        msg = await channel.send(embeds=embeds[:10])
        return int(msg.id)
    except discord.HTTPException:
        _LOG.exception("Failed to post catalog news to channel %s", channel_id)
        return None


async def run_catalog_news_sync(
    bot: discord.Client,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    api_key: str | None,
    site_origin: str,
    lookback_days: int = 60,
    min_cards_existing_set: int = 5,
    max_sets_per_run: int = 8,
    discord_channel_id: int | None = None,
    post_discord: bool = True,
) -> CatalogNewsRunResult:
    """Sync recent TCG sets, create announcements, optionally post to Discord."""
    result = CatalogNewsRunResult()
    recent = await fetch_sets_released_since(api_key=api_key, days=lookback_days)
    if not recent:
        result.messages.append("No recent sets from Pokémon TCG API.")
        return result

    set_ids = [s.set_id for s in recent[: max(1, int(max_sets_per_run))]]
    result.sets_checked = len(set_ids)

    sync_result = await sync_curated_sets(
        session_factory,
        set_ids=set_ids,
        api_key=api_key,
    )
    deltas = sync_result.deltas
    result.sets_synced = len([d for d in deltas.values() if d.new_cards or d.updated_cards])

    try:
        created_series = await sync_pack_series_from_catalog(session_factory)
        if created_series:
            result.messages.append(f"Pack series auto-sync created {created_series}.")
    except Exception:
        _LOG.exception("pack series sync after catalog news run")

    announcements: list[CatalogAnnouncement] = []
    async with session_factory() as session:
        for stats in deltas.values():
            kind = await _should_announce(
                session,
                stats,
                min_cards_existing_set=min_cards_existing_set,
            )
            if kind is None:
                continue
            row = await _create_announcement(
                session,
                kind=kind,
                stats=stats,
                site_origin=site_origin,
            )
            if row is not None:
                announcements.append(row)
        await session.commit()
        for row in announcements:
            await session.refresh(row)

    result.announcements_created = len(announcements)
    if not announcements:
        result.messages.append("Sync finished — no new catalog announcements.")
        return result

    if post_discord and discord_channel_id is not None:
        async with session_factory() as session:
            for row in announcements:
                msg_id = await post_discord_announcement(
                    bot,
                    channel_id=int(discord_channel_id),
                    row=row,
                )
                if msg_id is not None:
                    db_row = await session.get(CatalogAnnouncement, row.id)
                    if db_row is not None:
                        db_row.discord_message_id = msg_id
                        db_row.discord_channel_id = int(discord_channel_id)
                    result.discord_posts += 1
            await session.commit()

    for row in announcements:
        result.messages.append(f"Announced {row.title} ({row.new_card_count} cards).")
    return result
