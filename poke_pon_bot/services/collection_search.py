"""Text search helpers for owned-collection queries (website collection page)."""

from __future__ import annotations

from sqlalchemy import ColumnElement, String, and_, func, not_, or_

from poke_pon_bot.models.card import Card


def collection_text_search_clause(q: str | None) -> ColumnElement[bool] | None:
    """Match owned-card rows when ``q`` hits name, TCG type, supertype, set, or subtype.

    Returns ``None`` when ``q`` is empty (caller should omit the filter).
    """
    text = (q or "").strip().lower()
    if not text:
        return None
    pattern = f"%{text}%"
    json_like = lambda col: and_(col.isnot(None), func.lower(func.cast(col, String)).like(pattern))
    return or_(
        func.lower(Card.name).like(pattern),
        func.lower(func.coalesce(Card.supertype, "")).like(pattern),
        func.lower(func.coalesce(Card.set_name, "")).like(pattern),
        func.lower(func.coalesce(Card.set_code, "")).like(pattern),
        json_like(Card.tcg_types),
        json_like(Card.tcg_subtypes),
    )


def collection_text_search_negated_clause(q: str | None) -> ColumnElement[bool] | None:
    """Inverse of :func:`collection_text_search_clause` (e.g. evolution-line extras)."""
    clause = collection_text_search_clause(q)
    if clause is None:
        return None
    return not_(clause)
