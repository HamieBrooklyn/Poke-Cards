"""Resolve and manage global / per-guild ``/cd`` drop themes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from poke_pon_bot.models.card import Card
from poke_pon_bot.models.card_series import CardSeries, CardSeriesSet
_LOG = logging.getLogger(__name__)

from poke_pon_bot.models.cd_drop_theme import (
    THEME_KIND_POKEMON,
    THEME_KIND_SERIES,
    THEME_KIND_SET,
    THEME_KINDS,
    CdDropTheme,
)


@dataclass(frozen=True)
class ResolvedCdDropTheme:
    scope_label: str
    kind: str
    value: str
    chance_percent: int
    set_codes: tuple[str, ...] | None
    name_contains: str | None
    display: str


@dataclass(frozen=True)
class ThemeDrawConstraints:
    set_codes: tuple[str, ...] | None
    name_contains: str | None


def theme_applies_for_slot(chance_percent: int, rng) -> bool:
    pct = int(chance_percent)
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    return rng.random() < (pct / 100.0)


async def get_theme_row(
    session: AsyncSession,
    *,
    guild_id: int | None,
) -> CdDropTheme | None:
    return await session.scalar(
        select(CdDropTheme).where(CdDropTheme.guild_id.is_(guild_id))
    )


async def resolve_active_cd_drop_theme(
    session: AsyncSession,
    guild_id: int | None,
) -> ResolvedCdDropTheme | None:
    """Guild theme wins when present; otherwise global (``guild_id`` NULL)."""
    row: CdDropTheme | None = None
    scope_label = "global"
    if guild_id is not None:
        row = await get_theme_row(session, guild_id=guild_id)
        if row is not None:
            scope_label = f"server {guild_id}"
    if row is None:
        row = await get_theme_row(session, guild_id=None)
        scope_label = "global"
    if row is None or int(row.chance_percent) <= 0:
        return None
    try:
        return await _resolve_row(session, row, scope_label=scope_label)
    except ValueError:
        _LOG.warning(
            "Invalid stored cd drop theme scope=%s kind=%s value=%s",
            scope_label,
            row.theme_kind,
            row.theme_value,
            exc_info=True,
        )
        return None


async def _resolve_row(
    session: AsyncSession,
    row: CdDropTheme,
    *,
    scope_label: str,
) -> ResolvedCdDropTheme:
    kind = row.theme_kind.strip().lower()
    value = row.theme_value.strip()
    if kind not in THEME_KINDS:
        raise ValueError(f"Unknown theme kind {kind!r}.")
    constraints = await theme_constraints_for_kind(session, kind=kind, value=value)
    display = _format_display(kind, value, constraints)
    return ResolvedCdDropTheme(
        scope_label=scope_label,
        kind=kind,
        value=value,
        chance_percent=int(row.chance_percent),
        set_codes=constraints.set_codes,
        name_contains=constraints.name_contains,
        display=display,
    )


async def theme_constraints_for_kind(
    session: AsyncSession,
    *,
    kind: str,
    value: str,
) -> ThemeDrawConstraints:
    raw = value.strip()
    if not raw:
        raise ValueError("Theme value cannot be empty.")
    k = kind.strip().lower()
    if k == THEME_KIND_POKEMON:
        return ThemeDrawConstraints(set_codes=None, name_contains=raw)
    if k == THEME_KIND_SET:
        code = raw.lower()
        count = await session.scalar(
            select(func.count())
            .select_from(Card)
            .where(func.lower(Card.set_code) == code)
        )
        if not count:
            raise ValueError(f"No catalog cards with set code `{raw}`.")
        return ThemeDrawConstraints(set_codes=(code,), name_contains=None)
    if k == THEME_KIND_SERIES:
        series_code = raw.lower()
        series = await session.scalar(
            select(CardSeries).where(func.lower(CardSeries.code) == series_code)
        )
        if series is None:
            raise ValueError(
                f"No pack series with code `{raw}` — use `/dev grant_pack` series list or a `set` theme."
            )
        result = await session.execute(
            select(CardSeriesSet.set_code)
            .where(CardSeriesSet.series_id == series.id)
            .order_by(CardSeriesSet.set_code)
        )
        codes = tuple(sorted({str(r[0]).lower() for r in result.all() if r[0]}))
        if not codes:
            raise ValueError(f"Series `{raw}` has no linked TCG sets in the catalog.")
        return ThemeDrawConstraints(set_codes=codes, name_contains=None)
    raise ValueError(f"Unknown theme kind {kind!r}.")


def _format_display(kind: str, value: str, constraints: ThemeDrawConstraints) -> str:
    if kind == THEME_KIND_POKEMON:
        return f"Pokémon name contains **{value}**"
    if kind == THEME_KIND_SET:
        return f"TCG set **`{value}`**"
    if kind == THEME_KIND_SERIES and constraints.set_codes:
        sets_preview = ", ".join(f"`{c}`" for c in constraints.set_codes[:4])
        extra = ""
        if len(constraints.set_codes) > 4:
            extra = f" (+{len(constraints.set_codes) - 4} more)"
        return f"Pack series **`{value}`** ({sets_preview}{extra})"
    return value


async def validate_theme_config(
    session: AsyncSession,
    *,
    kind: str,
    value: str,
) -> ThemeDrawConstraints:
    k = kind.strip().lower()
    if k not in THEME_KINDS:
        raise ValueError(f"**kind** must be one of: {', '.join(sorted(THEME_KINDS))}.")
    return await theme_constraints_for_kind(session, kind=k, value=value)


async def upsert_cd_drop_theme(
    session: AsyncSession,
    *,
    guild_id: int | None,
    kind: str,
    value: str,
    chance_percent: int,
    updated_by_discord_user_id: int,
) -> CdDropTheme:
    await validate_theme_config(session, kind=kind, value=value)
    pct = max(0, min(100, int(chance_percent)))
    now = datetime.now(UTC)
    k = kind.strip().lower()
    v = value.strip()
    row = await get_theme_row(session, guild_id=guild_id)
    if row is None:
        row = CdDropTheme(
            guild_id=guild_id,
            theme_kind=k,
            theme_value=v,
            chance_percent=pct,
            updated_at=now,
            updated_by_discord_user_id=updated_by_discord_user_id,
        )
        session.add(row)
    else:
        row.theme_kind = k
        row.theme_value = v
        row.chance_percent = pct
        row.updated_at = now
        row.updated_by_discord_user_id = updated_by_discord_user_id
    await session.flush()
    return row


async def clear_cd_drop_theme(session: AsyncSession, *, guild_id: int | None) -> bool:
    result = await session.execute(delete(CdDropTheme).where(CdDropTheme.guild_id.is_(guild_id)))
    return (result.rowcount or 0) > 0


def format_theme_status_line(theme: ResolvedCdDropTheme | None) -> str:
    if theme is None:
        return ""
    return (
        f"🎨 **Drop theme** ({theme.scope_label}) — {theme.display} · "
        f"**{theme.chance_percent}%** of each `/cd` card"
    )


async def format_stored_theme_summary(
    session: AsyncSession,
    row: CdDropTheme | None,
    *,
    scope_name: str,
) -> str:
    if row is None:
        return f"**{scope_name}:** *(none)*"
    try:
        resolved = await _resolve_row(session, row, scope_label=scope_name)
    except ValueError as exc:
        return f"**{scope_name}:** `{row.theme_kind}` `{row.theme_value}` @ **{row.chance_percent}%** — ⚠️ {exc}"
    return (
        f"**{scope_name}:** {resolved.display} · **{resolved.chance_percent}%** "
        f"(`{resolved.kind}` / `{resolved.value}`)"
    )
